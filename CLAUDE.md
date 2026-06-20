# cc_repair_self — Claude Code 自优化系统

两台机器（opc_uname ↔ opc2_uname）通过此共享 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。

## 你的身份

你是 **opc2_uname** 机器上的 Claude Code。你的任务是分析本机 cc-infra 配置，发现可优化的点，修改配置并测试，push 回仓库。 opc_uname 也会拉取后优化本机。

**默认沟通语言：中文。**

## 架构（R35 dispatcher + 蓝绿 CC proxy + 自优化框架）

```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (model 字段路由 + 连接失败自动 fallback)
      ├── opus/未知 → :40005 proxy (EXPERIMENT, NV-enabled)  → MS-NV interleaving
      │   [40005 连接失败 → 自动 fallback 到 40001]
      └── sonnet    → :40001 proxy (STABLE, 纯 MS)  → MS-only
      │   [40001 连接失败 → 自动 fallback 到 40005]

:40005  cc-proxy(experiment)  → _cc /v1/messages → Anthropic→OpenAI 转换 → pure MS glm5.1 v×k cycling (NV disabled R35.2)
:40001  cc-proxy(stable)     → _cc /v1/messages → Anthropic→OpenAI 转换 → pure MS glm5.1 v×k cycling (NV disabled R35.2)
:40002  codex-proxy         → _cx /v1/responses  → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  openai-proxy        → _ol/_oc/_hm chat/completions → OpenAI passthrough → dsv4p v×k cycling

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep + dsv4pv1k1~v10k7 = 70 dep = 140 dep) → ModelScope
→ :7894 mihomo ♻️US-NV url-test (5 best US nodes) → NVIDIA integrate API (z-ai/glm-5.1, deepseek-ai/deepseek-v4-pro)
```

**proxy gateway 职责**：格式转换(CC/Codex)/透传(OpenAI agents) + metrics logging + MS-NV interleaving + variant×key 2D round-robin + error cycling。**不做压缩、不做截断**。LiteLLM 纯转发。NV API 直接通过 HTTPS CONNECT tunnel 调用（不经 LiteLLM）。

**R35 Dispatcher 自动 fallback**：40000 在连接 model-chosen 上游失败时，自动尝试另一个上游。日志记录 fallback 事件。不影响 model 路由逻辑。

**R35 蓝绿自优化**：40005 (experiment) 接收所有 opus/默认流量，承载最新参数/代码；40001 (stable) 是基线。`compare_proxies.sh` + `proxy_health_score.py` 对比两者表现；`auto_tune.sh` + `TUNE_RULES.md` 驱动参数调整。40005 表现优 → 版本提升到 40001；40005 表现差 → 回滚到 40001 基线。

**R33.2 MS-NV 交织规则**（R35.2已禁用NV interleaving on 40001/40005 — NV glm-5.1 API unavailable, 20s timeout）：原12 slot round-robin（7 MS + 5 NV），现改为纯MS模式。40003(openai-proxy)仍保留NV interleaving（NV_NUM_KEYS=5）。NV API对deepseek-v4-pro可用，对glm-5.1不可用。

## Agent Suffix System（R23.1, R29）

- `{base}{suffix}`：`glm5.1_cc`(Anthropic) / `glm5.1_cx`(Responses/Codex) / `dsv4p_ol`,`dsv4p_oc`,`dsv4p_hm`(OpenAI passthrough)
- 向后兼容：`glm5.1`=glm5.1_cc, `claude-opus-4-8`=glm5.1_cc, `glm5.1_ol`=dsv4p_ol

## Variant×Key 2D Round-Robin + Error Cycling（R21→R31.9）

- counter 持久化到文件 `rr_counter.json`（R30/R31.3），重启/断电不归零，每次 increment 立即 atomic 落盘
- request N → `variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS` → model `{base}v{V}k{K}`（行优先；R31.9 对角线实验证伪已回退）
- 429/500/502 时：**同 variant 换下一个 key**（k→k+1，ModelScope 每种错误都扣 quota，cycling 避免浪费）
- **R31.8: 7 key 全 429 → 立即终止返回错误（ABORT-NO-FALLBACK），不再 variant fallback**（曾 17x 放大，软件 bug 会耗尽账号）
- 错误返回格式按 agent 类型：_cc=Anthropic / _ol,_oc,_hm=OpenAI / _cx=Responses
- 不 cycling 的错误：400 input overflow / 400 inappropriate content / 400 thinking_budget InvalidParameter / 401/403

**R31.9 出站节流**：`MIN_OUTBOUND_INTERVAL_S=2.0`，`throttle_outbound()` 在每个 `conn.request` 前强制上次发送≥2s（缓解 ModelScope RPM burst throttle，根因见下）。env var 可调，设 0 禁用。

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
| frontend model_name | `glm5.1_cc`/`dsv4p_ol`/`dsv4p_oc`/`dsv4p_hm`/`glm5.1_cx` + 向后兼容 |
| LiteLLM model_name | `glm5.1v1k1`~`glm5.1v10k7` + `dsv4pv1k1`~`dsv4pv10k7` |
| Docker containers | `ms_uni41001`, `cc_postgres`, `auth_to_api_40000/40001/40002/40003/40005` |
| port assignments | 40000=dispatcher(+auto-fallback), 40001=cc(stable/baseline), 40002=codex, 40003=openai, 40005=cc(experiment/NV), 41001=LiteLLM, 7894=mihomo NV proxy |
| PROXY_ROLE per container | 40001/40005=cc, 40002=codex, 40003=passthrough，不可混淆 |
| NV proxy port | 7894（mihomo ♻️US-NV），NV_PROXY_URL 必须指向此端口 |

### 10 Variant Model IDs（ms_uni41001）

**GLM-5.1 (10):** `ZHIPUAI/GLM-5.1`, `ZHIPUAI/GLm-5.1`, `ZHIPUAI/GlM-5.1`, `ZHIPUAI/Glm-5.1`, `ZHIPUAI/gLM-5.1`, `ZHIPUAI/gLm-5.1`, `ZHIPUAI/glM-5.1`, `ZHIPUAI/glm-5.1`, `ZHIPUAi/GLM-5.1`, `ZHIPUAi/GLm-5.1`

**DSv4P (10):** `deepseek-ai/deepseek-v4-pro`, `...Deepseek-V4-Pro`, `...DeepSeek-v4-pro`, `...DeepSeek-v4-Pro`, `...DeepSeek-V4-PrO`, `...DeepSeek-V4-PRo`, `...DeepSeeK-V4-Pro`, `...DeepSeEk-V4-Pro`, `...DeepSEek-V4-Pro`, `...DeePSeek-V4-Pro`

## 关键原则（长期知识）

- **429 根因是 RPM burst throttle，不是 quota 耗尽**（R31.8/R31.9 核实）— 配额常剩 97% 但仍 429，因 ModelScope sliding-window token-bucket "这一秒打太快"。burst 自动恢复（~15min）。缓解靠 proxy throttle 2s 间隔，不可配置消除。
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
- **dsv4p 不支持 thinking_budget/reasoning_effort** — openai-proxy(40003) 自动 strip。
- **NV API 不支持 thinking_budget/reasoning_effort** — cc-proxy(40005) 对 NV calls 自动 strip（返回 400 "Unsupported parameter(s)"）。
- **NV API 必须经美国代理** — 直连 35+ 秒（glm5.1），美国代理 2-5 秒。NV_PROXY_URL=7894 专用 mihomo ♻️US-NV url-test。
- **NV API 连续请求会变慢** — burst 排队效应，需 ~2-3s 间隔。MIN_OUTBOUND_INTERVAL_S=2.0 对 NV 也生效。
- **NV 不经 LiteLLM** — LiteLLM v1.87 不支持 HTTPS_PROXY env（aiohttp transport 忽略），也不支持 litellm_params.proxy（400 error）。cc-proxy 用 HTTPS CONNECT tunnel 直连 NV API。

## 可调整参数（有数据支撑才能改）

### Proxy env（docker-compose.yml）

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | 1.5-6 | token 估算 |
| MODEL_INPUT_TOKEN_SAFETY_GLM51 | 170000 | 120-190k | glm5.1 context_window |
| MODEL_INPUT_TOKEN_SAFETY_DSV4P | 128000 | 64-128k | dsv4p context_window |
| PROXY_TIMEOUT | 300 | 120-600 | proxy 请求超时秒 |
| UPSTREAM_TIMEOUT | 60 | 30-120 | per-key HTTPConnection 超时 |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | 0-5 | **R35.2 出站节流秒数（1.5s validated: 429率30%, TTFB 5.0s, 0 ABORT）** |
| NUM_VARIANTS_GLM51/DSV4P | 10 | 5-10 | 每 backend variant 数 |
| NV_NUM_KEYS | 0(40001/40005), 5(40003) | 0-5 | R35.2: 40001/40005 NV disabled (glm-5.1 unavailable); 40003 still NV-enabled |
| NV_PROXY_URL | host.docker.internal:7894 | — | mihomo ♻️US-NV 专用美国代理端口 |
| MS_NV_TOTAL_SLOTS | 12(40003), N/A(40001/40005) | 7-12 | R35.2: 40001/40005 pure MS; 40003 still 7MS+5NV interleaving |
| NV_TIMEOUT | 20 | 10-60 | R35.1: NV-specific connection timeout (NV成功avg5s med4.4s max17s) |

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

### LiteLLM router_settings（41001，140 dep）

`num_retries=0` / `cooldown_time=10` / `routing_strategy=simple-shuffle` / 所有 `*AllowedFails=0`（proxy 处理全部 cycling，LiteLLM 纯转发）

## 项目文件结构

```
configs/
  docker-compose.yml          # Docker 编排（cc_postgres, ms_uni41001, auth_to_api_40000/001/002/003/005）
  .env.template
  litellm-glm51/config.yaml   # 41001 LiteLLM（70 glm5.1 + 70 dsv4p = 140 dep）
  mihomo/config-opc_uname.yaml # opc_uname mihomo 代理配置（7894=♻️US-NV, 7880=mixed, etc.）
  postgres/init-db.sh
  TUNE_RULES.md               # R35: 参数自动调整规则表（安全边界）
  PROXY_HEALTH_SCORES.md      # R35: 自动生成的健康评分（compare_proxies.sh 产出）
  NEXT_ROUND.md               # R35: 优化循环接力文件
  proxy/
    dispatcher/               # 40000 路由+自动 fallback（R35，按 model 字段 + 连接失败容错）
    cc-proxy/                 # 40001+40005 共用（R31.5 物理拆分，R35 蓝绿统一镜像）
      Dockerfile + gateway/{app,config,handlers,upstream,converters,stream,error_mapping,logger}.py
    codex-proxy/              # 40002（R31.6 拆分）
    openai-proxy/             # 40003（R31.6 拆分）
  claude/
    settings-opc_uname.json / settings-opc2_uname.json   # → 各机 ~/.claude/settings.json
  DEPLOY_STATUS.md
scripts/
  backup_config.sh / deploy.sh / health_check.sh / restart_claude.sh / rollback.sh / sync_config.sh / switch_cc_proxy.sh / ts_keepalive.sh / run_optimization_loop.sh
  compare_proxies.sh           # R35: 40001 vs 40005 metrics 对比分析
  proxy_health_score.py        # R35: 综合健康评分计算 + PROXY_HEALTH_SCORES.md 生成
  auto_tune.sh                 # R35: 参数自动寻优（--dry-run/--apply/--suggest 三种模式）
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

# NV API 直连测试（经美国代理7894）
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
  -d '{"model":"dsv4p_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# role isolation — 40005/40001 should reject /v1/chat/completions (404)
```

## 网络代理（opc_uname mihomo）

```bash
systemctl --user start mihomo.service      # mihomo 代理服务（所有端口）
systemctl --user restart mihomo.service     # 配置变更后重启

# mihomo 端口分配（opc_uname）
# 7880  mixed port    — 通用代理（自动选择最佳节点）
# 7891  🇸🇬狮城节点    — 新加坡专用
# 7892  🇯🇵日本节点    — 日本专用
# 7893  ♻️US自动       — 美国自动（url-test）
# 7894  ♻️US-NV       — NVIDIA API 专用美国代理（5 best US nodes url-test）
# 9090  API 控制面板    — mihomo external-controller
```
