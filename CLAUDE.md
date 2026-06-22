# cc_repair_self — Claude Code 自优化系统

两台机器（opc_uname ↔ opc2_uname）通过此共享 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。

## 你的身份

你是 **opc2_uname** 机器上的 Claude Code。你的任务是分析本机 cc-infra 配置，发现可优化的点，修改配置并测试，push 回仓库。 opc_uname 也会拉取后优化本机。

**默认沟通语言：中文。**

## 架构（R36.2 dispatcher + 蓝绿 CC proxy + MS-NV strict alternating + 自优化框架）

```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (model 字段路由 + 连接失败自动 fallback)
      ├── opus/未知 → :40005 proxy (EXPERIMENT, MS-NV strict alternating, NV_NUM_KEYS=5)
      │   [40005 连接失败 → 自动 fallback 到 40001]
      └── sonnet    → :40001 proxy (STABLE, 纯 MS)  → MS-only
      │   [40001 连接失败 → 自动 fallback 到 40005]

:40005  cc-proxy(experiment) → _cc /v1/messages → strict MS-NV alternating (ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→ms→nv)
  NV slot: single-key attempt, per-key proxy URL (7894-7899), NV_TIMEOUT=60s, sock.settimeout() after conn.request()
  NV failure → immediate MS switch; MS failure → ABORT-NO-FALLBACK; Empty 200 → NV failure
:40001  cc-proxy(stable)     → _cc /v1/messages → pure MS glm5.1 v×k cycling (NV disabled, baseline)
:40002  codex-proxy          → _cx /v1/responses  → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  openai-proxy         → _ol/_oc/_hm chat/completions → OpenAI passthrough → glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."; SSE buffer-based parsing (FR 85.7%)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope
→ :41101-41105 LiteLLM ms_nv_4110X (1 NV key each, in-memory 2GiB, monitoring only)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 (region-divided url-test, tolerance=0) → NVIDIA integrate API (z-ai/glm-5.1)
```

**proxy gateway 职责**：格式转换(CC/Codex)/透传(OpenAI agents) + metrics logging + MS-NV strict alternating + variant×key 2D round-robin + error cycling。**不做压缩、不做截断**。LiteLLM 纯转发。NV API 直接通过 HTTPS CONNECT tunnel 调用（不经 LiteLLM）。

**R36 MS-NV strict alternating**：12 slots(7MS+5NV), counter%12 → even=MS, odd=NV。NV single-key attempt(no cycling), 失败→immediate MS switch(NV-MS-SWITCH)。MS failure→ABORT-NO-FALLBACK(no NV fallback)。cycle counter n+1 atomic落盘, NV_MAX_CYCLE=1200000。

**R35 Dispatcher 自动 fallback**：40000 在连接 model-chosen 上游失败时，自动尝试另一个上游。日志记录 fallback 事件。不影响 model 路由逻辑。

**R35 蓝绿自优化**：40005 (experiment) 接收所有 opus/默认流量，承载最新参数/代码；40001 (stable) 是基线。`compare_proxies.sh` + `proxy_health_score.py` 对比两者表现；`auto_tune.sh` + `TUNE_RULES.md` 驱动参数调整。40005 表现优 → 版本提升到 40001；40005 表现差 → 回滚到 40001 基线。

## Agent Suffix System（R23.1, R29）

- `{base}{suffix}`：`glm5.1_cc`(Anthropic) / `glm5.1_cx`(Responses/Codex) / `glm5.1_ol`(OpenClaw) / `glm5.1_oc`(OpenCode) / `glm5.1_hm`(Hermes)
- 向后兼容：`glm5.1`=glm5.1_cc, `claude-opus-4-8`=glm5.1_cc, `glm5.1_ol`=glm5.1_ol

## Variant×Key 2D Round-Robin + Error Cycling（R21→R31.9）

- counter 持久化到文件 `rr_counter.json`（R30/R31.3），重启/断电不归零，每次 increment 立即 atomic 落盘
- request N → `variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS` → model `{base}v{V}k{K}`（行优先；R31.9 对角线实验证伪已回退）
- 429/500/502 时：**同 variant 换下一个 key**（k→k+1，ModelScope 每种错误都扣 quota，cycling 避免浪费）
- **R31.8: 7 key 全 429 → 立即终止返回错误（ABORT-NO-FALLBACK），不再 variant fallback**（曾 17x 放大，软件 bug 会耗尽账号）
- 错误返回格式按 agent 类型：_cc=Anthropic / _ol,_oc,_hm=OpenAI / _cx=Responses
- 不 cycling 的错误：400 input overflow / 400 inappropriate content / 400 thinking_budget InvalidParameter / 401/403

**R31.9 出站节流**：`MIN_OUTBOUND_INTERVAL_S=1.5`（R35.8从2.0→1.5），`throttle_outbound()` 在每个 `conn.request` 前强制上次发送≥1.5s（缓解 ModelScope RPM burst throttle，根因见下）。env var 可调，设 0 禁用。

## CC 内置 429 重试机制（逆向自 claude.exe 原生二进制，非外部脚本）

CC 收到 429 会自动退避重试，对用户透明（"7key 全 429 你无感知"的原因）：
- 429 走 `h = HP5(err) ?? min(r7H(attempt, retryAfter, cap=30000), EF6)`
- **有 retry-after header** → `parseInt(retryAfter)` 秒（我们发 `retry-after:5` → 每次等 5s）
- **无 retry-after** → 指数退避 0.5s→1s→2s（bL5=500）
- **retry-after > 60s → 抛 too_long，CC 直接放弃不重试**（180s 的 quota-exhausted 用此路径，让用户看到错误）
- 重试上限 SL5=2（重试 2 次仍 429 → 抛 api_request_retry_exhausted）
- **proxy 不能阻止 CC 重试，只能用 retry-after 调节奏**

## 不可变更约束（NEVER CHANGE）

| 约束 | 原因 |
|------|------|
| **所有 variant model IDs** | 每个变体有独立 200/id/天额度，**绝对禁止增删改** |
| **rpm=1 per deployment** | 每个 deployment 限速 1 RPM，**绝对禁止修改** |
| frontend model_name | `glm5.1_cc`/`glm5.1_ol`/`glm5.1_oc`/`glm5.1_hm`/`glm5.1_cx` + 向后兼容 |
| LiteLLM model_name | `glm5.1v1k1`~`glm5.1v10k7` |
| Docker containers | `ms_uni41001`, `cc_postgres`, `ms_nv_41101-41105`, `auth_to_api_40000/40001/40002/40003/40005` |
| port assignments | 40000=dispatcher(+auto-fallback), 40001=cc(stable), 40002=codex, 40003=openai, 40005=cc(experiment/NV), 41001=LiteLLM MS, 41101-41105=LiteLLM NV K1-K5, 7894-7899=mihomo NV proxy per-key |
| PROXY_ROLE per container | 40001/40005=cc, 40002=codex, 40003=passthrough，不可混淆 |
| NV proxy ports | 7894-7899（mihomo ♻️US-NV-K1~K5, protocol-based url-test + nv_proxy_selector.sh IP diversity），NV_PROXY_URL_MAP per-key |

### 10 Variant Model IDs（ms_uni41001）

**GLM-5.1 (10):** `ZHIPUAI/GLM-5.1`, `ZHIPUAI/GLm-5.1`, `ZHIPUAI/GlM-5.1`, `ZHIPUAI/Glm-5.1`, `ZHIPUAI/gLM-5.1`, `ZHIPUAI/gLm-5.1`, `ZHIPUAI/glM-5.1`, `ZHIPUAI/glm-5.1`, `ZHIPUAi/GLM-5.1`, `ZHIPUAi/GLm-5.1`

## 关键原则（长期知识）

- **429 根因是 RPM burst throttle，不是 quota 耗尽**（R31.8/R31.9 核实）— 配额常剩 97% 但仍 429，因 ModelScope sliding-window token-bucket "这一秒打太快"。burst 自动恢复（~15min）。缓解靠 proxy throttle 1.5s 间隔，不可配置消除。
- **429 是 HTTP 状态码逐跳透传**（非 body 字符串）。body 的 `"code":"429"` 字段不参与分类。
- **variant/key 都不是瓶颈**（R31.9 对角线实验）— 41 次 ABORT 均匀分布 v1~v10。换 variant 无效，靠间隔。
- **删除资源前必须验证其独立价值** — 曾删 11 个变体以为是"不支持的混合大小写"，实际每个有独立 200/id/day 额度。
- **proxy 绝不做截断/压缩** — proxy-level auto-compact 导致灾难性上下文丢失。
- **429→529 转换会致 CC 崩溃** — 529=overloaded 触发 CC auto-compact→上下文丢失。绝不转换。
- **proxy 不做 retry** — proxy-level retry 增加 37% 延迟（15963ms vs 11635ms）。
- **CC auto-compact 质量远低于手动 /compact** — 自动用 stripNonEssential=true（截断 tool 输出），手动更完整。
- **CC tokenizer 高估 ~1.7x**（中文+代码+JSON 混合）— autoCompactWindow 需考虑此偏差。
- **CC env 优先级**：settings.json 的 env 块 > webui 系统env > shell env。改链路必须改 settings.json。
- **CC v2.1.170+ startup check 用 shell env**（不读 settings.json）— 需 .bashrc + .profile + restart 脚本 bash --login 三层保障。
- **/health 触发 LiteLLM fd 耗尽** — 用 /health/liveliness 监控 LiteLLM；proxy 的 /health 安全。
- **ModuleNotFoundError → proxy 崩溃 → CC ConnectionRefused 卡死**（R27）— 改 import 后确认 .py 存在。
- **代码改了必须 rebuild 容器**（proxy 代码 COPY 进镜像非 mount）— `--build --force-recreate` 才生效。
- **NV API 不支持 thinking_budget/reasoning_effort** — cc-proxy(40005) 对 NV calls 自动 strip（返回 400 "Unsupported parameter(s)"）。
- **NV API 必须经美国代理** — 直连 35+ 秒（glm5.1），美国代理 2-5 秒。R36.3: per-key proxy(NV_PROXY_URL_MAP), protocol-based分组(K1=Hysteria2, K2=Vless三网, K3/4=Vless0.1倍, K5=全池), nv_proxy_selector.sh 保证IP多样性。
- **NV 代理节点池只有 ~17 个独立出口 IP** — Hysteria2 8个(pq.us1-8), Vless三网推荐 7个(134.195.101.x), 其他共享CF CDN。协议分流(K1=Hy2, K2=Vless三网)自然保证IP多样性。204延迟测试≠NV推理延迟（gstatic 204只测TCP+TLS建连，NV推理层可能独立超时）。
- **NV API 连续请求会变慢** — burst 排队效应，需 ~2-3s 间隔。MIN_OUTBOUND_INTERVAL_S=1.5 对 NV 也生效。
- **http.client timeout只控connect不控read** — HTTPSConnection.timeout只控TCP+SSL+CONNECT，**不控getresponse()**。必须用 `conn.sock.settimeout(NV_TIMEOUT)` after `conn.request()`（R36.2 critical fix）。
- **NV 不经 LiteLLM** — LiteLLM v1.87 不支持 HTTPS_PROXY env（aiohttp transport 忽略），也不支持 litellm_params.proxy（400 error）。cc-proxy 用 HTTPS CONNECT tunnel 直连 NV API。
- **OpenClaw auto-compact 会截断 messages 以 assistant 结尾** — GLM 5.1 API 拒绝这种序列 → "Cannot continue from message role: assistant" → 整个 session 失败。R35.10: passthrough proxy 自动追加 user "Continue." 修复序列。

## 可调整参数（有数据支撑才能改）

### Proxy env（docker-compose.yml）

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | 1.5-6 | token 估算 |
| MODEL_INPUT_TOKEN_SAFETY_GLM51 | 170000 | 120-190k | glm5.1 context_window |
| PROXY_TIMEOUT | 300 | 120-600 | proxy 请求超时秒 |
| UPSTREAM_TIMEOUT | 60 | 30-120 | per-key HTTPConnection 超时 |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | 0-5 | **R35.8: 全部 proxy 对齐 1.5s（40003 从 2.0→1.5）** |
| NUM_VARIANTS_GLM51 | 10 | 5-10 | glm5.1 variant 数 |
| NV_NUM_KEYS | 5 (40005) / 0 (40001/40003) | 0-5 | R36: 40005 strict MS-NV alternating; 40001/40003 pure MS baseline |
| NV_PROXY_URL | host.docker.internal:7894 | — | mihomo ♻️US-NV-K1 (fallback single-key proxy) |
| NV_PROXY_URL_MAP | {0:7894,1:7895,2:7896,3:7897,4:7899} | — | R36.2: per-key proxy URL for fault isolation + IP diversity |
| MS_NV_TOTAL_SLOTS | 12 (7MS+5NV on 40005) | 7-12 | R36: strict alternating, even=MS, odd=NV |
| NV_TIMEOUT | 60 | 10-60 | R36: increased from 20→60; sock.settimeout() after conn.request() for read timeout |
| NV_MAX_CYCLE | 1200000 | — | Cycle counter reset threshold |

### CC settings（claude/settings-*.json）

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| contextWindow | 170000 | 120-190k | CC 上下文容量上限 |
| autoCompactWindow / CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | 90-180k | auto-compact 触发阈值 |
| API_TIMEOUT_MS | 600000 | 300-1200k | CC→proxy HTTP 总超时 |

### retry-after（proxy handlers.py，控制 CC 重试节奏）

| 场景 | 值 | CC 行为 |
|------|-----|---------|
| transient 429（7key 全 429）| 5 | CC 等 5s 重试，多数 burst 透明恢复 |
| quota 429（非 transient）| 180 | >60s → CC 直接放弃，报错给用户 |

### LiteLLM router_settings（41001，70 dep）

`num_retries=0` / `cooldown_time=10` / `routing_strategy=simple-shuffle` / 所有 `*AllowedFails=0`（proxy 处理全部 cycling，LiteLLM 纯转发）

## 项目文件结构

```
configs/
  docker-compose.yml          # Docker 编排（R36.2: 12 containers, YAML anchors 1CPU/1-2GiB, Docker official proxy）
  .env.template
  litellm-glm51/config.yaml   # 41001 LiteLLM（70 glm5.1 = 70 dep）
  litellm-nv/config-k1~k5.yaml # 41101-41105 NV LiteLLM（1 dep each, in-memory）
  mihomo/config-opc_uname.yaml # opc_uname mihomo 代理配置（7894-7899=♻️US-NV-K1~K5, 7880=mixed）
  mihomo/config-opc2_uname.yaml # opc2_uname mihomo 配置
  postgres/init-db.sh
  TUNE_RULES.md               # R35: 参数自动调整规则表（安全边界）
  PROXY_HEALTH_SCORES.md      # R35: 自动生成的健康评分
  NEXT_ROUND.md               # R35: 优化循环接力文件
  proxy/
    dispatcher/               # 40000 路由+自动 fallback
    cc-proxy/                 # 40001+40005 共用（蓝绿统一镜像）
      Dockerfile + gateway/{app,config,handlers,upstream,converters,stream,error_mapping,logger}.py
    codex-proxy/              # 40002
    openai-proxy/             # 40003
  claude/
    settings-opc_uname.json / settings-opc2_uname.json   # → 各机 ~/.claude/settings.json
  DEPLOY_STATUS.md
scripts/
  backup_config.sh / deploy.sh / health_check.sh / restart_claude.sh / rollback.sh / sync_config.sh / switch_cc_proxy.sh / ts_keepalive.sh / run_optimization_loop.sh
  compare_proxies.sh / proxy_health_score.py / auto_tune.sh / nv_proxy_selector.sh
```

## 持久化自优化 loop（cron `*/30`）

`scripts/run_optimization_loop.sh` 每 30 分钟由 cron 唤起一个 headless agent（`claude -p --dangerously-skip-permissions`），按 `memory/cron-optimization-loop.md` 流程执行一轮（读 `configs/NEXT_ROUND.md` 接力 → 采集日志/quota → 有数据才改 → 写回接力）。

**R35 增强工具链**：
- `compare_proxies.sh`: 对比 40001(stable) vs 40005(experiment) 的 429率/TTFB/成功率/NV使用率
- `proxy_health_score.py`: 计算综合健康分（0-100），写入 `PROXY_HEALTH_SCORES.md`
- `auto_tune.sh`: 按 `TUNE_RULES.md` 规则自动调整参数（安全边界内）
  - `--dry-run`: 只预览，不修改
  - `--suggest`: 写建议到 NEXT_ROUND.md
  - `--apply`: 直接修改 docker-compose.yml（仅小范围安全参数）

**蓝绿迭代**：40005(experiment) → 数据验证 → 版本提升到 40001(stable)，或回滚到 40001 基线。

## 关键文件路径与重启

| 文件 | 路径 | 修改后 |
|------|------|--------|
| LiteLLM 配置 | `/opt/cc-infra/litellm-glm51/config.yaml` | `docker restart ms_uni41001` |
| cc-proxy 代码 | `/opt/cc-infra/proxy/cc-proxy/gateway/` | `docker compose up -d --build --force-recreate auth_to_api_40005`（或 40001）|
| dispatcher | `/opt/cc-infra/proxy/dispatcher/` | recreate `auth_to_api_40000` |
| docker-compose.yml / .env | `/opt/cc-infra/` | recreate 相关容器 |
| mihomo 配置 | `~/.config/mihomo/config.yaml` | `systemctl --user restart mihomo.service` 或 API reload |
| Claude settings | `~/.claude/settings.json` | 重启 claude 进程 |

```bash
# 仅重建 cc-proxy 40005（primary）
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40005
# 全量重建
cd /opt/cc-infra && docker compose up -d --force-recreate
```

## 每轮优化协议

1. **拉取** `git pull` → 2. **分析** 读 metrics/error_detail 日志（用数据核实，不看表象）→ 3. **计划** 说明 WHY + 日志证据 → 4. **备份** `bash scripts/backup_config.sh` → 5. **执行** 改配置 + rebuild 受影响容器 → 6. **测试** curl 验证 200 → 7. **验证** 读新 metrics 对比 → 8. **记录** DEPLOY_STATUS.md + memory → 9. **Push**

## 测试请求

```bash
# CC (_cc) via 40005 (primary) — MS-NV interleaving 链路
curl -s -X POST http://127.0.0.1:40005/v1/messages \
  -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# CC with thinking_budget (NV slot will auto-strip)
curl -s -X POST http://127.0.0.1:40005/v1/messages \
  -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50,"thinking_budget":5000}'

# NV API 直连测试（经美国代理7894-7899, per-key）
curl -s -x http://127.0.0.1:7894 -X POST https://integrate.api.nvidia.com/v1/chat/completions \
  -H "Authorization: Bearer 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" \
  -H "Content-Type: application/json" \
  -d '{"model":"z-ai/glm-5.1","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' --max-time 30

# Codex (_cx) via 40002
curl -s -X POST http://127.0.0.1:40002/v1/responses \
  -H "Authorization: Bearer sk-litellm-local" -d '{"model":"glm5.1_cx","input":"test"}'

# OpenAI agents via 40003
curl -s -X POST http://127.0.0.1:40003/v1/chat/completions \
  -H "Authorization: Bearer sk-litellm-local" \
  -d '{"model":"glm5.1_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# role isolation — 40005/40001 should reject /v1/chat/completions (404)
```

## 网络代理（opc_uname / opc2_uname mihomo）

```bash
systemctl --user start mihomo.service      # mihomo 代理服务（所有端口）
systemctl --user restart mihomo.service     # 配置变更后重启

# mihomo 端口分配（opc_uname）
# 7880  mixed port    — 通用代理（自动选择最佳节点）
# 7891  🇸🇬狮城节点    — 新加坡专用
# 7892  🇯🇵日本节点    — 日本专用
# 7893  ♻️US自动       — 美国自动（url-test）
# 7894  ♻️US-NV-K1    — Hysteria2最快 (8 unique pq.us servers, ~165ms, NV Key 1)
# 7895  ♻️US-NV-K2    — Vless三网推荐 (7 unique 134.195.101.x IPs, ~170ms, NV Key 2)
# 7896  ♻️US-NV-K3    — Vless0.1倍率 (CF CDN, exclude 三网/电信联通, NV Key 3)
# 7897  ♻️US-NV-K4    — Vless池 tolerance=30 (exclude 三网/电信联通, NV Key 4)
# 7899  ♻️US-NV-K5    — 全池 tolerance=0 (全部美国节点最快, NV Key 5 fallback)
# 9090  API 控制面板    — mihomo external-controller
```
