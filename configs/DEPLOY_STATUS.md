# Deploy Status — opc_uname + opc2_uname (R29, 2026-06-18)

## Architecture (R29 — Three-proxy 分治 + dual backend model)
```
Agent(CC/_cc)      → 40001(proxy, PROXY_ROLE=cc, Anthropic format conversion + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [glm5.2 backend]
Agent(Codex/_cx)   → 40002(proxy, PROXY_ROLE=codex, Responses API→Chat conversion + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [glm5.2 backend]
Agent(OpenClaw/_ol) → 40003(proxy, PROXY_ROLE=passthrough, OpenAI passthrough + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [dsv4p backend]
Agent(OpenCode/_oc) → 40003(proxy, PROXY_ROLE=passthrough, OpenAI passthrough + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [dsv4p backend]
Agent(Hermes/_hm)  → 40003(proxy, PROXY_ROLE=passthrough, OpenAI passthrough + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [dsv4p backend]
```

Proxy does **format conversion (CC→Anthropic, Codex→Responses API) / passthrough (OpenAI agents) + variant×key 2D round-robin + variant fallback (R23) + error cycling (429/500/502) + metrics logging** for ALL agent types.

**R29 Key Changes**:
- **Three proxy containers**: 40001(cc) + 40002(codex) + 40003(passthrough) — same Docker image, differentiated by PROXY_ROLE env var
- **Dual backend model**: CC/Codex→glm5.2, OpenAI agents→dsv4p (DeepSeek V4 Pro)
- **DSv4P restored**: 10 variants × 7 keys = 70 deployments (R24 removed, R29 restored with independent 200/id/day quota)
- **LiteLLM fallback removed**: No ms_uni41002, no proxy-level fallback. Only ms_uni41001 (140 dep: 70 glm5.2 + 70 dsv4p)
- **Strict endpoint isolation**: Each proxy only serves its role's endpoint (cc→/v1/messages, codex→/v1/responses, passthrough→/v1/chat/completions), others→404

**Variant×Key 2D Round-Robin + Variant Fallback (R21→R23, R29 dual model)**:
- request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- glm5.2: → model name `glm5.2v{V}k{K}` (e.g. glm5.2v1k1)
- dsv4p: → model name `dsv4pv{V}k{K}` (e.g. dsv4pv1k1)
- Error cycling (429/500/502): same variant, next key (k→k+1). All 7 keys failed → **R23: try 2 fallback variants (1 key each)** before returning to agent
- After all fallbacks fail → classify and return to agent (all-429→rate_limit **retry-after=180s**; has-500/502→api_error; has-timeout→502)
- Each variant has independent 200/id/day quota on ModelScope

**R29: dsv4p does NOT support thinking_budget/reasoning_effort** — passthrough proxy (40003) strips these params automatically.

## Containers (R29)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM | 70 glm5.2 dep + 70 dsv4p dep = 140 dep, ulimits nofile=2048, memory 1536MiB |
| auth_to_api_40001 | :40001 | Proxy (cc) | PROXY_ROLE=cc, /v1/messages only, backend=glm5.2 |
| auth_to_api_40002 | :40002 | Proxy (codex) | PROXY_ROLE=codex, /v1/responses only, backend=glm5.2 |
| auth_to_api_40003 | :40003 | Proxy (passthrough) | PROXY_ROLE=passthrough, /v1/chat/completions only, backend=dsv4p |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine (litellm_glm51 DB only) |

## Deploy Method (R29+)
```bash
# ms_uni41001 config change → restart only
docker restart ms_uni41001

# proxy change → rebuild ALL 3 proxy containers
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002 auth_to_api_40003

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```

**opc2_uname R27 HOTFIX DEPLOYED 2026-06-13 01:44 CST**：
- **根因**: handlers.py 引用 `gateway.codex` 模块但 codex.py 文件不存在 → proxy 启动时 ModuleNotFoundError → 容器无限 Restarting → CC 连接 proxy 时 ConnectionRefused → 卡死
- **修复**: 创建 codex.py 模块（Responses API → Chat Completions 格式转换） + 同步所有 R26/R27 改动
- **验证**: 4容器全部 healthy, curl 40001/40002 glm5.2_cc→200 ✅, glm5.2_ol→200 ✅, glm5.2_cx→200 ✅

**opc2_uname R28 GLM-5.2 UPGRADE DEPLOYED 2026-06-17**：
- **变更**: 模型从 GLM-5.1 升级到 GLM-5.2（opc2_uname 用户手动升级）
- **发现的问题**: proxy容器未重建 + CC settings model未同步 + Git仓库未同步
- **修复**: rebuild proxy容器 + 更新CC settings + 同步Git仓库(glm5.1→glm5.2)

**opc2_uname R29 THREE-PROXY + DSV4P RESTORE — DEPLOYED 2026-06-18 01:30 CST**：
- **变更**: 三proxy容器分治(40001=cc, 40002=codex, 40003=passthrough) + dsv4p恢复(70 dep) + LiteLLM fallback去掉(ms_uni41002删除)
- **部署过程**:
  1. git pull + sync_config.sh → 所有config文件同步到/opt/cc-infra
  2. docker restart ms_uni41001 → 140 dep配置加载
  3. docker stop + docker rm ms_uni41002 → fallback LiteLLM移除
  4. docker compose up -d --build --force-recreate → 5容器重建
  5. hotfix: ProxyHandler import位置修正(NameError crash → 移入main()内部)
- **验证**: 5容器全部healthy, curl测试全部通过:
  - glm5.2 via 40001 (Anthropic) → 200 ✅
  - glm5.2_cx via 40002 (Responses API) → 200 ✅
  - dsv4p_ol via 40003 (OpenAI) → 200 ✅
  - glm5.2_ol backward compat → dsv4p ✅
  - 40001 rejects /v1/chat/completions → 404 ✅
  - 40003 rejects /v1/messages → 404 ✅

## Current Parameters (R29)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY_GLM51 | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| MODEL_INPUT_TOKEN_SAFETY_DSV4P | 128000 | docker-compose.yml | Reported to OpenAI agents via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Both containers running 3.0 ✅ |
| NUM_KEYS | 7 | docker-compose.yml | Keys per model for round-robin |
| NUM_VARIANTS_GLM51 | 10 | docker-compose.yml | Variants per key group for glm5.2 |
| NUM_VARIANTS_DSV4P | 10 | docker-compose.yml | R29: Variants per key group for dsv4p |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Overall request timeout concept (seconds) |
| UPSTREAM_TIMEOUT | 60 | docker-compose.yml | Per-key HTTPConnection timeout (seconds) |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| PROXY_ROLE | cc/codex/passthrough | docker-compose.yml | Per-container role, determines endpoint filtering |
| timeout (ms_uni41001) | 300 | litellm config.yaml | Seconds |
| num_retries (ms_uni41001) | 0 | litellm config.yaml | Proxy handles all error cycling; LiteLLM pure pass-through |
| cooldown_time (ms_uni41001) | 10 | litellm config.yaml | — |
| routing_strategy (ms_uni41001) | simple-shuffle | litellm config.yaml | Proxy specifies exact model, LiteLLM just forwards |
| RateLimitErrorAllowedFails | 0 | litellm config.yaml | 429 cycling by proxy |
| TimeoutErrorAllowedFails | 0 | litellm config.yaml | timeout cycling by proxy |
| InternalServerErrorAllowedFails | 0 | litellm config.yaml | 500 cycling by proxy |
| API_TIMEOUT_MS | 600000 | settings.json | CC→proxy HTTP total timeout (10min) |

## Agent Suffix Model IDs (R29)

| Suffix | Agent | Format | Endpoint | Proxy Port | Backend Model | Error Cycling | thinking_budget |
|--------|-------|--------|----------|------------|----------------|---------------|-----------------|
| `_cc` | Claude Code | Anthropic→OpenAI conversion | /v1/messages | 40001 | glm5.2 | ✅ 429/500/502/timeout | ✅ supported |
| `_cx` | Codex | Responses API→Chat conversion | /v1/responses | 40002 | glm5.2 | ✅ 429/500/502/timeout | ✅ supported |
| `_ol` | OpenClaw | OpenAI passthrough | /v1/chat/completions | 40003 | dsv4p | ✅ 429/500/502/timeout | ❌ stripped |
| `_oc` | OpenCode | OpenAI passthrough | /v1/chat/completions | 40003 | dsv4p | ✅ 429/500/502/timeout | ❌ stripped |
| `_hm` | Hermes | OpenAI passthrough | /v1/chat/completions | 40003 | dsv4p | ✅ 429/500/502/timeout | ❌ stripped |

Frontend model IDs: `glm5.2_cc`, `dsv4p_ol`, `dsv4p_oc`, `dsv4p_hm`, `glm5.2_cx`
Backward compat: `glm5.2`=glm5.2_cc, `claude-opus-4-8`=glm5.2_cc, `glm5.2_ol`=dsv4p_ol, `glm5.2_oc`=dsv4p_oc, `glm5.2_hm`=dsv4p_hm, `codex-mini-latest`=glm5.2_cx

## 10 Variant Model IDs (ms_uni41001, R29 — glm5.2 + dsv4p)

**GLM-5.2 (ms_uni41001):** 10 variants × 7 keys = 70 deployments
`ZHIPUAI/GLM-5.2`, `ZHIPUAI/GLm-5.2`, `ZHIPUAI/GlM-5.2`, `ZHIPUAI/Glm-5.2`, `ZHIPUAI/gLM-5.2`, `ZHIPUAI/gLm-5.2`, `ZHIPUAI/glM-5.2`, `ZHIPUAI/glm-5.2`, `ZHIPUAi/GLM-5.2`, `ZHIPUAi/GLm-5.2`

**DSv4P (ms_uni41001, R29 restored):** 10 variants × 7 keys = 70 deployments
`deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**

## opc2_uname Remote Verification (R29, 2026-06-18)

**opc2_uname（远程机器）R29配置与仓库完全一致** ✅：
- 5个容器全部 healthy (ms_uni41001, cc_postgres, auth_to_api_40001, auth_to_api_40002, auth_to_api_40003)
- PROXY_ROLE隔离: 40001=cc, 40002=codex, 40003=passthrough ✅
- curl test glm5.2 via 40001 (Anthropic) → 200 ✅
- curl test glm5.2_cx via 40002 (Responses API) → 200 ✅
- curl test dsv4p_ol via 40003 (OpenAI) → 200 ✅
- curl test glm5.2_ol backward compat → dsv4p ✅
- Role isolation: 40001 rejects /v1/chat/completions → 404 ✅
- Role isolation: 40003 rejects /v1/messages → 404 ✅

## Log System Analysis (R22, 2026-06-12)

### Proxy日志（3层日志系统）

| 日志层 | 文件格式 | 内容 | 大小趋势 |
|--------|----------|------|----------|
| proxy.{date}.log | 纯文本 | 每请求一行简要日志（REQ/ERR/TIMEOUT等） | 0.2-0.6MB/天 |
| metrics.{date}.jsonl | JSON行 | 结构化metrics：request_id, model, ttfb_ms, tokens, variant_idx, key_idx, proxy_role | 0.2-2.5MB/天 |
| error_detail.{date}.jsonl | JSON行 | 详细错误：error_subcategory, upstream_error_body, key_cycle_attempts | 0-0.35MB/天 |

### ⚠️ 缺失：Proxy日志无自动清理机制
- proxy.py按日期写文件，**无rotation/purge/cleanup**
- 建议: 添加crontab任务，保留最近7天proxy日志

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality

### Variant×Key 2D Round-Robin (R21/R29) — CRITICAL deploy order
- **ms_uni41001 MUST be running first**: Proxy sends routing model names to LiteLLM. If LiteLLM doesn't have these → "Invalid model name" → CC crash
- **R29: LiteLLM now has 140 dep (70 glm5.2 + 70 dsv4p)**: ms_uni41001 must start with new config.yaml before proxy rebuild

### ⚠️ CRITICAL: Module import completeness check
- **Lesson from R27 hotfix**: When adding new module imports, the referenced module file MUST exist in the gateway directory before rebuilding the Docker container. Missing module → ModuleNotFoundError → container crash loop → ConnectionRefused → CC stuck.

### ⚠️ CRITICAL: R29 deploy sequence
- **Deploy order**: 1) Update .env on opc2_uname 2) Update litellm config.yaml + restart ms_uni41001 3) Rebuild all 3 proxy containers 4) Remove ms_uni41002 5) Update postgres (remove litellm_glm51_fallback DB) 6) Update agent configs (OpenClaw→40003/dsv4p_ol, etc.)
- **ms_uni41002 removal**: Must docker stop + docker rm ms_uni41002 before docker compose up -d --force-recreate
- **Memory increase**: ms_uni41001 needs 1536MiB (was 1024MiB for 70 dep, now 140 dep)

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by ms_requests_remaining). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked). Independent from RPM.

### /health endpoint — NEVER use on LiteLLM
- LiteLLM /health → per-deployment checks → fd exhaustion. Use /health/liveliness.
- Proxy /health → simple status check + proxy_role → SAFE for Docker healthcheck.

### R29: dsv4p thinking_budget stripping
- passthrough proxy (40003) automatically strips `reasoning_effort` and `thinking_budget` from dsv4p requests
- logged as "DSV4P-STRIP" for monitoring

## R30 (2026-06-18): opc_uname glm5.1→glm5.2 实机升级 + 远程IP变更

### 背景
- opc_uname (opcsname-1) 实机仍跑 glm5.1（settings.json model=glm5.1_cc，/opt/cc-infra proxy代码glm5.1，单容器只有 40001+cc_postgres+ms_uni41001）
- Git 仓库已是 glm5.2（R28/R29 已改名），仅 /opt/cc-infra 未同步
- 远程 IP 变更：opc_uname tailscale 100.113.71.43 → **100.109.153.83**，LAN 192.168.1.102 → **192.168.1.111**

### 执行步骤
1. `bash scripts/sync_config.sh`（远程）同步 repo glm5.2 配置到 /opt/cc-infra（docker-compose/litellm config/proxy gateway 全部 COPIED）
2. settings.json model: `glm5.1_cc` → `glm5.2_cc`
3. 全量重建：`rm -rf proxy/gateway/__pycache__` + `docker compose build --no-cache` + `up -d --force-recreate`（**关键：__pycache__ 残留会导致旧 .pyc 进镜像，必须先清**）
4. 验证：
   - 40001 (CC) glm5.2 → HTTP 200，返回 `"model": "glm5.2"`，变体 `glm5.2v1kX` ✅
   - 40002 (Codex) glm5.2_cx → KEY-CYCLE-SUCCESS on glm5.2v1k3 ✅
   - 40003 (Passthrough) dsv4p → 3/3 HTTP 200 ✅
   - 40001 role isolation → /v1/chat/completions 返回 404 ✅

### 配套变更
- 本机 `~/.ssh/config`: opc_uname Hostname LAN→192.168.1.111，tailscale→100.109.153.83
- `scripts/ts_keepalive.sh`: PEERS 改为 `100.109.153.83`（opcsname-1），移除已下线的 opcsname-2/desktop/android 节点

### 关键教训
- **Docker COPY + __pycache__ 残留陷阱**：`docker compose build` 即使加 `--no-cache`，如果 host `proxy/gateway/__pycache__` 残留旧 .pyc，`COPY gateway/` 会把旧 .pyc 一起打进镜像。Python 优先加载 .pyc → 镜像里代码看起来是新的，实际跑的是旧的（grep glm5.2=59 但运行仍报 glm5.1）。**修复：build 前必须 `rm -rf proxy/gateway/__pycache__`**。

## R30.1 (2026-06-18): glm5.2 v×k counter 均衡性修复 — counter 持久化 + monitor.sh 误重启修复

### 问题现象
日志分析发现 glm5.2 v1k1~v10k7 分布**极度不均衡**：
- v1=47次 vs v7/v8=7次（差 6.7 倍）
- 理论上 70 个 deployment 应最多只差 1 个请求
- 新版本 429 暴增，连环 cycling（v1k1~v1k7 全 429 + fallback 也 429）

### 根因（三个 bug 串联）
1. **Bug #1（主因）：counter 不持久化**
   - `gateway/config.py` 的 `_vk_rr_counter` 是纯内存 dict，容器每次重启归零
   - counter 逻辑本身正确（锁保护 + 单调递增 + `N→(N//7)%10, N%7`），只是没持久化
   - 重启后流量全部砸向 v1~v3，打爆 RPM quota → 429 storm

2. **Bug #2（放大器）：monitor.sh 每30分钟误判重建 40001**
   - `monitor.sh:215-217` grep `PROXY_HEALTHY=yes` / `LITELLM_GLM51_HEALTHY=yes` / `LITELLM_DSV4P_HEALTHY=yes`
   - 但 `health_check.sh` 实际输出 `PROXY_40001_HEALTHY=yes` / `LITELLM_HEALTHY=yes`
   - 三个 grep 永远匹配失败 → 每30分钟（cron `*/30 * * * *`）判定 unhealthy → `--force-recreate auth_to_api_40001`（只重建40001）
   - 证据：proxy.log 每整 :00/:30 有 `[START] Proxy listening`，共20次/天；docker RestartCount=0（不是docker restart，是 force-recreate）

3. **Bug #3：monitor.sh 调用不存在的容器名**
   - `docker restart glm5.1_uni41001` / `dsv4p_uni42001` 不存在（实际是 `ms_uni41001`）
   - `|| true` 静默吞错，说明 monitor.sh 是 R25 之前的陈旧版本

### 修复
- **config.py**：counter 持久化到 `$LOG_DIR/rr_counter.json`
  - import 时 `_load_rr_counter()` 恢复；每10次递增 `_save_rr_counter()` 落盘；`atexit.register` 注册退出保存
  - 用 `print(stderr)` 而非 `_log`，避免与 logger.py 的循环 import
- **monitor.sh**：修正 grep 字符串匹配真实输出；LiteLLM 重启目标改 `ms_uni41001`

### 验证
- counter 文件 `{"glm5.2": 10}`，重启后 startup 日志 `[RR-COUNTER] restored: {'glm5.2': 10}` ✅
- 重启后落点在 v2k5(N=11) 而非 v1k1 ✅
- monitor.sh 模拟判断：PROXY_OK=y GLM_OK=y DSV_OK=y → 不再误重建 ✅
- 角色隔离 404 正常；cycling（v3k2→v3k3→...）正常 ✅

### 注意
- `atexit` 在 `docker stop`(SIGTERM) 时不触发，仅靠每10次落盘；非整数边界重启最多丢9步（可接受，远好于全重置）
- 部署后短期 429/502 是 burst quota 被 storm 耗尽的临时状态，~15分钟自动恢复
