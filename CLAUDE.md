# cc_repair_self — Claude Code 自优化系统

两台机器（opc_uname ↔ opc2_uname）通过此共享 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。

## 你的身份

你是 **opc_uname** 机器上的 Claude Code。你的任务是拉取此仓库，分析 **opc2_uname**（远程机器）上的 cc-infra 配置，发现可优化的点，修改配置并测试，然后 push 回仓库让 opc2_uname 拉取继续优化。

## 架构

```
                    :40001 proxy (PROXY_ROLE=cc, CC only)
                    ├── _cc (Claude Code) → /v1/messages → Anthropic→OpenAI转换 → upstream.py glm5.2 v×k cycling

                    :40002 proxy (PROXY_ROLE=codex, Codex only)
                    ├── _cx (Codex CLI)   → /v1/responses → Responses→Chat转换 → upstream.py glm5.2 v×k cycling

                    :40003 proxy (PROXY_ROLE=passthrough, OpenAI agents only)
                    ├── _ol (OpenClaw)    → /v1/chat/completions → OpenAI passthrough → upstream.py dsv4p v×k cycling
                    ├── _oc (OpenCode)    → /v1/chat/completions → OpenAI passthrough → upstream.py dsv4p v×k cycling
                    ├── _hm (Hermes)      → /v1/chat/completions → OpenAI passthrough → upstream.py dsv4p v×k cycling

                    → :41001 LiteLLM ms_uni41001 (glm5.2v1k1~v10k7 = 70 dep + dsv4pv1k1~v10k7 = 70 dep = 140 dep)
                    → ModelScope API
```

核心原则：**proxy gateway做格式转换(CC/Codex) / 透传(OpenAI agents) + metrics logging + variant×key 2D round-robin + variant fallback (R23) + error cycling（所有agent类型共享upstream.py的v×k cycling和错误处理，429/500/502时同variant换下一个key，7 key全失败→尝试2个fallback variant（各1 key），fallback也失败才返回agent，retry-after=180s），不做压缩、不做截断。LiteLLM纯转发（proxy精确指定variant+key组合，num_retries=0，所有allowed_fails=0，避免浪费ModelScope quota）。压缩完全由CC内置auto-compact控制。**

**R29: 三容器分治架构**
- 3个proxy容器共享相同gateway代码（同一Docker镜像），通过PROXY_ROLE env var差异化
- 每个proxy只服务对应role的endpoint，其他endpoint返回404
- CC专用(40001) + Codex专用(40002) + 透传专用(40003)，互相独立
- LiteLLM fallback暂时去掉（只有ms_uni41001，无ms_uni41002）

**Agent Suffix System (R23.1, R29 backend routing update):**
- 模型ID格式：`{base_model}{agent_suffix}` → 如 `glm5.2_cc`, `dsv4p_ol`, `dsv4p_oc`, `dsv4p_hm`, `glm5.2_cx`
- 向后兼容：`glm5.2_ol`=dsv4p_ol（old suffix with glm5.2 base仍然路由到dsv4p backend）
- `_cc` → Anthropic格式（/v1/messages），需要格式转换 + force-stream-for-nonstream，backend=glm5.2
- `_cx` → Responses API格式（/v1/responses），Codex CLI专用，需要格式转换，backend=glm5.2
- `_ol/_oc/_hm` → OpenAI格式（/v1/chat/completions），直通passthrough，backend=dsv4p
- 所有agent共享相同的error cycling + variant fallback：429/500/502 key cycling + timeout cycling + thinking_budget fix retry
- 错误格式根据agent类型：_cc返回Anthropic格式错误，_ol/_oc/_hm返回OpenAI格式错误，_cx返回Responses API格式错误

**Variant×Key 2D Round-Robin + Variant Fallback (R21→R23, R29扩展到dsv4p):**
- Proxy 维护 2D轮换 counter：request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS → model `{base}v{V}k{K}`
- glm5.2轮询序列：v1k1→v1k2→...→v1k7→v2k1→v2k2→...→v10k7→回到v1k1
- dsv4p轮询序列：v1k1→v1k2→...→v1k7→v2k1→v2k2→...→v10k7→回到v1k1
- 429/500/502 时：同variant换下一个key（k→k+1），不变variant（ModelScope每种错误都扣quota，cycling避免浪费）
- 7 key 全部失败（同variant） → **R23: 尝试2个fallback variant（各试1个key），减少quota浪费的同时利用不同variant的独立RPM quota**
- R29: 不再有LiteLLM fallback（去掉ms_uni41002），所有key连接错误也走variant fallback
- 合并所有错误分类返回：全429→rate_limit_error（retry-after=180s）；有500→api_error；有502→api_error；有timeout→502 api_error
- 每个key的 cycling 尝试都记录在 error_detail 日志中（含variant_idx、error_type）
- 成功时：记录 variant_idx、key_idx 和 litellm_model，以及之前的 cycling 信息
- 不cycling的错误：400 input overflow、400 inappropriate content、400 thinking_budget InvalidParameter、401/403 AuthenticationError
- **R29: dsv4p不支持thinking_budget/reasoning_effort** — passthrough proxy(40003)会自动strip这些参数

## 不可变更约束（NEVER CHANGE）

| 约束 | 原因 |
|------|------|
| **所有 variant model IDs** | 每个变体model ID有独立额度200/id/天。删减变体=删减额度，**绝对禁止增删改**（R21用户主动删除dsv4p v11） |
| **rpm=1 per deployment** | 每个deployment限速1 RPM。**绝对禁止修改** |
| frontend model_name (agent-facing) | `glm5.2_cc`, `dsv4p_ol`, `dsv4p_oc`, `dsv4p_hm`, `glm5.2_cx` — R29 suffix system; backward compat: `glm5.2`=glm5.2_cc, `claude-opus-4-8`=glm5.2_cc, `glm5.2_ol`=dsv4p_ol |
| LiteLLM model_name (internal, R21) | `glm5.2v1k1`~`glm5.2v10k7` + `dsv4pv1k1`~`dsv4pv10k7` — proxy精确指定variant+key |
| Docker container names | `ms_uni41001`, `cc_postgres`, `auth_to_api_40001/40002/40003` |
| port assignments | 41001=LiteLLM(ms_uni41001), 40001=proxy(cc), 40002=proxy(codex), 40003=proxy(passthrough) |
| PROXY_ROLE per container | `auth_to_api_40001`=cc, `auth_to_api_40002`=codex, `auth_to_api_40003`=passthrough — 不可混淆 |

### 10 Variant Model IDs（R21/R29, ms_uni41001）

**GLM-5.2 (10 variants):**
`ZHIPUAI/GLM-5.2`, `ZHIPUAI/GLm-5.2`, `ZHIPUAI/GlM-5.2`, `ZHIPUAI/Glm-5.2`, `ZHIPUAI/gLM-5.2`, `ZHIPUAI/gLm-5.2`, `ZHIPUAI/glM-5.2`, `ZHIPUAI/glm-5.2`, `ZHIPUAi/GLM-5.2`, `ZHIPUAi/GLm-5.2`

**DSv4P (10 variants, R29 restored):**
`deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`

## 关键原则（长期知识）

- **删除资源前必须验证其独立价值** — 曾删除11个变体model ID以为是"不支持的混合大小写"，但每个变体有独立200/id/day额度。正确流程：观察→测试→验证→决定。R24删了dsv4p，R29恢复（因为需要独立后端模型）。
- **proxy-level retry增加37%延迟** — 有proxy_retry的请求avg=15963ms vs 正常11635ms。proxy只做格式转换，retry由LiteLLM负责。
- **proxy绝不做截断/压缩** — proxy-level auto-compact导致灾难性上下文丢失。压缩只由CC内置auto-compact控制。
- **429→529 转换会导致CC崩溃** — 429=rate_limit(backoff retry)，529=overloaded(CC auto-compact→灾难性上下文丢失)。绝不转换。
- **CC v2.1.170+ startup check用shell env vars** — CC启动时的connectivity check用shell环境变量（ANTHROPIC_BASE_URL等），不读settings.json。必须三层保障：.bashrc（在non-interactive return之前）+.profile+restart_claude.sh用bash --login。
- **CC auto-compact质量远低于手动/compact** — 自动compact用stripNonEssential=true（截断tool输出，tools=[]），手动/compact用stripNonEssential=false（完整上下文）。CC提示compact时，主动/compact加自定义指令可获得更好的摘要。
- **/health endpoint会触发fd耗尽** — LiteLLM的/health触发per-deployment checks→OSError Too many open files。用/health/liveliness监控LiteLLM。Proxy的/health是简单状态检查（只返回{"status":"ok"}+proxy_role），SAFE用于Docker healthcheck。
- **CC tokenizer overestimates tokens ~1.7x** — 对中文+代码+JSON混合内容，Anthropic tokenizer估算值比ModelScope实际值高约1.7倍。autoCompactWindow必须考虑此偏差。
- **ModelScope双 quota 系统** — RPM quota（200/id/day/variant）和 Token quota（per-key hourly/daily）是独立的。burst是暂时性的（15分钟自动恢复），不可通过配置修复。ModelScope对每种错误都扣quota，所以必须在proxy层做error cycling。
- **多CC进程加速token quota耗尽** — R23修复：variant fallback(2个额外variant各1key) + retry-after=180s(3分钟) + kill多余CC进程
- **ModuleNotFoundError → proxy崩溃 → CC ConnectionRefused卡死** — R27教训：修改import后必须确认被引用的.py文件存在于gateway目录中。
- **proxy超时日志详细记录** — socket.timeout单独捕获，记录elapsed_ms、proxy_timeout_setting_ms、timeout_exceeded_by_ms。
- **R29: dsv4p不支持thinking_budget** — passthrough proxy(40003)会自动strip reasoning_effort和thinking_budget参数，避免ModelScope 400 InvalidParameter错误。

## 可调整参数（有数据支撑才能改）

### Proxy / Docker-compose.yml env

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | 1.5-6 | proxy用CPT估算tokens |
| MODEL_INPUT_TOKEN_SAFETY_GLM51 | 170000 | 120000-190000 | /v1/models报告的glm5.2 context_window |
| MODEL_INPUT_TOKEN_SAFETY_DSV4P | 128000 | 64000-128000 | /v1/models报告的dsv4p context_window |
| MAX_TOOL_DESC | 2000 | 800-4000 | 工具描述截断上限chars |
| MAX_SCHEMA_DESC | 600 | 300-1200 | Schema描述截断上限chars |
| PROXY_TIMEOUT | 300 | 120-600 | proxy请求超时秒 |
| PROXY_ROLE | cc/codex/passthrough | 固定 | 每个proxy容器固定role，不可动态切换 |

### CC settings (claude/settings-*.json)

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| contextWindow | 170000 | 120000-190000 | CC认知的上下文容量上限 |
| autoCompactWindow | 155000 | 90000-180000 | CC自动compact触发阈值（est tokens） |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | 90000-180000 | env var，与autoCompactWindow对齐 |
| API_TIMEOUT_MS | 600000 | 300000-1200000 | CC→proxy HTTP总超时 |

### LiteLLM router_settings (41001 ms_uni41001, 140 dep = 70 glm5.2 + 70 dsv4p)

| 参数 | 当前值 | 说明 |
|------|--------|------|
| num_retries | 0 | proxy处理所有error cycling，LiteLLM纯转发 |
| cooldown_time | 10 | RPM 1-min窗口 |
| routing_strategy | simple-shuffle | proxy精确指定model，LiteLLM只转发 |
| RateLimitErrorAllowedFails | 0 | 429 cycling由proxy处理 |
| TimeoutErrorAllowedFails | 0 | timeout cycling由proxy处理 |
| InternalServerErrorAllowedFails | 0 | 500 cycling由proxy处理 |
| AuthenticationErrorAllowedFails | 0 | |
| BadRequestErrorAllowedFails | 0 | |

### Proxy / Docker-compose.yml env (R21新增, R29扩展)

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| NUM_VARIANTS_GLM51 | 10 | 5-10 | glm5.2每个key group的variant数 |
| NUM_VARIANTS_DSV4P | 10 | 5-10 | dsv4p每个key group的variant数 |
| UPSTREAM_TIMEOUT | 60 | 30-120 | R27: Per-key HTTPConnection超时（秒） |

## 项目文件结构

```
configs/
  docker-compose.yml       # Docker编排（5个容器：cc_postgres, ms_uni41001, auth_to_api_40001/40002/40003）
  .env.template             # 环境变量模板
  litellm-glm51/config.yaml       # 41001 LiteLLM配置（10v×7k glm5.2 = 70 dep + 10v×7k dsv4p = 70 dep = 140 dep total）
  postgres/init-db.sh             # PostgreSQL初始化脚本
  proxy/
    Dockerfile                    # 镜像构建
    gateway_main.py               # 入口
    gateway/                      # R23.1 模块化gateway包
      __init__.py                 # 包导出
      app.py                      # 入口（ThreadedHTTPServer + main + PROXY_ROLE log）
      config.py                   # 配置 + AGENT_SUFFIXES + PROXY_ROLE + detect_agent_type() + dsv4p routing
      handlers.py                 # 请求调度 + PROXY_ROLE endpoint过滤
      upstream.py                 # 共享v×k cycling + variant fallback + error handling（R29: 无LiteLLM fallback）
      converters.py               # Anthropic↔OpenAI格式转换 + 文本估算
      stream.py                   # SSE流转换（Anthropic格式）
      error_mapping.py            # 错误格式转换（Anthropic convert_error + OpenAI format_* + Responses format_*）
      codex.py                    # R24: Responses API→Chat Completions 格式转换 + 流转换
      logger.py                   # 日志（_log, _log_metrics, _log_error_detail）
  claude/
    settings-opc_uname.json        # → opc_uname本机 ~/.claude/settings.json
    settings-opc2_uname.json       # → opc2_uname远程 ~/.claude/settings.json
    statusline-command-opc_uname.sh / statusline-command.sh
  DEPLOY_STATUS.md                 # 当前部署状态
scripts/
  backup_config.sh / deploy.sh / health_check.sh / restart_claude.sh / rollback.sh / sync_config.sh / ts_keepalive.sh
```

## 关键文件路径（opc_uname 本机 / opc2_uname 远程）

| 文件 | 路径 | 修改后需 |
|------|------|----------|
| LiteLLM 配置 | `/opt/cc-infra/litellm-glm51/config.yaml` | `docker restart ms_uni41001` |
| 转换代理 | `/opt/cc-infra/proxy/gateway/` | rebuild + recreate ALL proxy容器 |
| Docker Compose | `/opt/cc-infra/docker-compose.yml` | `docker compose up -d --force-recreate` |
| 环境变量 | `/opt/cc-infra/.env` | recreate相关容器 |
| Claude设置 | `~/.claude/settings.json` | 重启claude进程 |
| Shell env vars | `.bashrc` + `.profile` + `/etc/environment` | 新终端生效 |

## 重启命令

```bash
# LiteLLM 配置变更（ms_uni41001）
docker restart ms_uni41001

# proxy.py 变更（需要重建镜像，影响所有3个proxy容器）
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002 auth_to_api_40003

# 全量重建
cd /opt/cc-infra && docker compose up -d --force-recreate

# Claude Code 重启
bash ~/cc_ps/cc_recover/restart_claude.sh
```

## 每轮优化协议

1. **拉取** — `git pull`
2. **分析** — 读metrics/error_detail日志
3. **计划** — 必须说明WHY，附日志证据
4. **备份** — `bash scripts/backup_config.sh`
5. **执行** — 修改配置，重启受影响容器
6. **测试** — curl验证glm5.2和dsv4p返回200
7. **验证** — 读新metrics，对比前后
8. **记录** — 更新DEPLOY_STATUS.md
9. **Push** — 推送到GitHub

## 测试请求

```bash
# Anthropic格式 — CC (_cc) via 40001
curl -s -X POST http://127.0.0.1:40001/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-litellm-local" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.2","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# Responses API — Codex (_cx) via 40002
curl -s -X POST http://127.0.0.1:40002/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-litellm-local" \
  -d '{"model":"glm5.2_cx","input":"test"}'

# OpenAI格式 — OpenClaw/OpenCode/Hermes (_ol/_oc/_hm) via 40003
curl -s -X POST http://127.0.0.1:40003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-litellm-local" \
  -d '{"model":"dsv4p_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# OpenAI格式 — backward compat (glm5.2_ol still routes to dsv4p)
curl -s -X POST http://127.0.0.1:40003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-litellm-local" \
  -d '{"model":"glm5.2_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# Verify role isolation — 40001 should reject /v1/chat/completions
curl -s -X POST http://127.0.0.1:40001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-litellm-local" \
  -d '{"model":"test","messages":[{"role":"user","content":"test"}],"max_tokens":50}'
# Expected: 404 "CC proxy only serves /v1/messages"
```

## 网络代理（opc_uname 本机如需）

```bash
systemctl --user start mihomo-sg.service
# SSH已配置~/.ssh/config自动通过443端口+代理
git push  # 自动走代理
```
