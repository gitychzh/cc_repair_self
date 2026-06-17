# Deploy Status — opc_uname + opc2_uname (R27, 2026-06-13)

## Architecture (R27 — glm5.2 only + Codex + LiteLLM fallback)
```
Agent(CC/_cc)      → 40001/40002(proxy, Anthropic format conversion + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [UNIFIED, glm5.2 only]
    → 41002 ms_uni41002 LiteLLM → ModelScope [FALLBACK, same config] (R26: connection error fallback)
Agent(OpenClaw/_ol) → 40001/40002(proxy, OpenAI passthrough + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [UNIFIED, glm5.2 only]
Agent(OpenCode/_oc) → 40001/40002(proxy, OpenAI passthrough + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [UNIFIED, glm5.2 only]
Agent(Hermes/_hm)  → 40001/40002(proxy, OpenAI passthrough + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [UNIFIED, glm5.2 only]
Agent(Codex/_cx)   → 40001/40002(proxy, Responses API → Chat Completions conversion + v×k 2D round-robin + variant fallback + error cycling)
    → 41001 ms_uni41001 LiteLLM → ModelScope [UNIFIED, glm5.2 only]
```

Proxy does **format conversion (CC→Anthropic, Codex→Responses API) + passthrough (OpenAI agents) + variant×key 2D round-robin + variant fallback (R23) + LiteLLM fallback (R26) + error cycling (429/500/502) + metrics logging** for ALL agent types.

**R26 LiteLLM Fallback**: When ALL keys in start variant fail with connection errors (ConnectionRefused/ConnectionError/SocketTimeout), proxy automatically tries fallback LiteLLM container (ms_uni41002) with same v×k cycling. Only triggers on connection errors — 429/500/502 are ModelScope issues (same keys = same quota on both containers).

**Variant×Key 2D Round-Robin + Variant Fallback (R21→R23)**:
- request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- → model name: `glm5.2v{V}k{K}` (e.g. glm5.2v1k1)
- Error cycling (429/500/502): same variant, next key (k→k+1). All 7 keys failed → **R23: try 2 fallback variants (1 key each)** before returning to agent
- Variant fallback (R23): All 7 keys 429 in start variant → try (start_variant+1) and (start_variant+2), each with 1 key attempt. Max extra quota waste = 2 keys per request
- **R26 LiteLLM fallback**: All 7 keys connection errors → try ms_uni41002 with same v×k cycling before returning to agent
- After all fallbacks fail → classify and return to agent (all-429→rate_limit **retry-after=180s**; has-500/502→api_error; has-timeout→502)
- Each variant has independent 200/id/day quota on ModelScope

**R27: UPSTREAM_TIMEOUT separated from PROXY_TIMEOUT**:
- UPSTREAM_TIMEOUT=60s: Per-key HTTPConnection timeout (how long to wait for each individual key attempt)
- PROXY_TIMEOUT=300s: Overall request concept timeout (for docs/reference)
- This separation allows fine-tuning per-key timeout independently from total request timeout

**R24: All agents route to glm5.2 only**. opus/sonnet/haiku/mini aliases all → glm5.2 (dsv4p removed entirely).

## Containers (R27)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM (primary) | 7 groups × 10 variants = 70 dep (glm5.2 only), ulimits nofile=2048, memory 1GiB |
| ms_uni41002 | :41002 | Unified LiteLLM (fallback) | R26: Same config as ms_uni41001, fallback for connection errors |
| auth_to_api_40001 | :40001 | Proxy (primary) | R27 v×k 2D round-robin + variant fallback + LiteLLM fallback → ms_uni41001/41002 |
| auth_to_api_40002 | :40002 | Proxy (secondary) | R27 v×k 2D round-robin + variant fallback + LiteLLM fallback → ms_uni41001/41002 |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine (only litellm_glm51 DB) |

## Deploy Method (R27+)
```bash
# ms_uni41001 config change → restart only
docker restart ms_uni41001

# proxy change → rebuild (need new Dockerfile build)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```

**opc2_uname R27 HOTFIX DEPLOYED 2026-06-13 01:44 CST**：
- **根因**: handlers.py 引用 `gateway.codex` 模块但 codex.py 文件不存在 → proxy 启动时 ModuleNotFoundError → 容器无限 Restarting → CC 连接 proxy 时 ConnectionRefused → 卡死
- **修复**: 创建 codex.py 模块（Responses API → Chat Completions 格式转换） + 同步所有 R26/R27 改动（UPSTREAM_TIMEOUT, LiteLLM fallback, _cx suffix）
- **验证**: 4容器全部 healthy, curl 40001/40002 glm5.2_cc→200 ✅, glm5.2_ol→200 ✅, glm5.2_cx→200 ✅

## Current Parameters (R27)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Both containers running 3.0 ✅ |
| NUM_KEYS | 7 | docker-compose.yml | Keys per model for round-robin |
| NUM_VARIANTS_GLM51 | 10 | docker-compose.yml | R21: variants per key group for glm5.2 |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Overall request timeout concept (seconds) |
| UPSTREAM_TIMEOUT | 60 | docker-compose.yml | R27: Per-key HTTPConnection timeout (seconds) |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (ms_uni41001) | 300 | litellm config.yaml | Seconds |
| num_retries (ms_uni41001) | 0 | litellm config.yaml | R22: proxy handles all error cycling; LiteLLM pure pass-through |
| cooldown_time (ms_uni41001) | 10 | litellm config.yaml | — |
| routing_strategy (ms_uni41001) | simple-shuffle | litellm config.yaml | Proxy specifies exact model, LiteLLM just forwards |
| RateLimitErrorAllowedFails | 0 | litellm config.yaml | R22: 429 cycling by proxy, LiteLLM no retry (avoid wasting quota) |
| TimeoutErrorAllowedFails | 0 | litellm config.yaml | R22: timeout cycling by proxy |
| InternalServerErrorAllowedFails | 0 | litellm config.yaml | R22: 500/choice:null cycling by proxy |
| API_TIMEOUT_MS | 600000 | settings.json | R22: CC→proxy HTTP total timeout (10min) |

## opc2_uname Remote Verification (R27, 2026-06-13)

**opc2_uname（远程机器）所有配置与仓库完全一致** ✅：
- gateway module: 10 files synced (app.py, config.py, converters.py, codex.py, error_mapping.py, handlers.py, __init__.py, logger.py, stream.py, upstream.py)
- docker-compose.yml: R27 version (5 containers, ms_uni41002 fallback LiteLLM)
- litellm-glm51/config.yaml: R24 version (70 dep glm5.2 only)
- 5个容器全部 healthy (ms_uni41001, ms_uni41002, cc_postgres, auth_to_api_40001, auth_to_api_40002)
- Proxy env vars: NUM_KEYS=7, NUM_VARIANTS_GLM51=10, PROXY_TIMEOUT=300, UPSTREAM_TIMEOUT=60
- CC settings.json: model=glm5.2_cc, API_TIMEOUT_MS=600000 ✅
- curl test glm5.2_cc via 40001 returns 200 ✅
- curl test glm5.2_ol via 40001 returns 200 ✅
- curl test glm5.2_cx via 40001 returns 200 ✅ (R24 Codex Responses API)

**opc2_uname CC auto-update fix (2026-06-12)**：
- **根因**: opc2_uname 没有 `~/.npmrc` → npm 默认 prefix=/usr (root所有) → CC auto-update 无法写入 → `nowrite permission to npm prefix`
- **对比本机**: opc_uname 有 `~/.npmrc` (prefix=/home/opc_uname/.npm-global) → npm prefix 在用户目录 → 可写入 → auto-update 正常
- **修复**: 创建 opc2_uname `~/.npmrc` (prefix=/home/opc2_uname/.npm-global) → npm prefix 指向用户目录 → 可写入 ✅

**opc2_uname 可达 via tailscale**: SSH `opc2sname-tailscale:222` ✅

## Log System Analysis (R22, 2026-06-12)

### Proxy日志（3层日志系统）

| 日志层 | 文件格式 | 内容 | 大小趋势 |
|--------|----------|------|----------|
| proxy.{date}.log | 纯文本 | 每请求一行简要日志（REQ/ERR/TIMEOUT等） | 0.2-0.6MB/天 |
| metrics.{date}.jsonl | JSON行 | 结构化metrics：request_id, model, ttfb_ms, tokens, variant_idx, key_idx | 0.2-2.5MB/天 |
| error_detail.{date}.jsonl | JSON行 | 详细错误：error_subcategory, upstream_error_body, key_cycle_attempts | 0-0.35MB/天 |

### ⚠️ 缺失：Proxy日志无自动清理机制
- proxy.py按日期写文件，**无rotation/purge/cleanup**
- 建议: 添加crontab任务，保留最近7天proxy日志

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality

### Variant×Key 2D Round-Robin (R21) — CRITICAL deploy order
- **ms_uni41001 MUST be running first**: Proxy sends `glm5.2v1k1` to LiteLLM. If LiteLLM doesn't have these names → "Invalid model name" → CC crash

### ⚠️ CRITICAL: Module import completeness check
- **Lesson from R27 hotfix**: When adding new module imports (e.g. `from .codex import handle_codex_responses`), the referenced module file MUST exist in the gateway directory before rebuilding the Docker container. Missing module → ModuleNotFoundError → container crash loop → ConnectionRefused → CC stuck.
- **Always verify**: After modifying handlers.py imports, check that all referenced .py files exist in the gateway directory before deploying.

### LiteLLM Fallback (R26) — Connection Error Resilience
- **Trigger condition**: ALL keys in start variant fail with ConnectionRefused/ConnectionError/SocketTimeout
- **Does NOT trigger on**: 429/500/502 (ModelScope issues — same keys = same quota on both containers)
- **Fallback container**: ms_uni41002 (same config as ms_uni41001, port :41002)
- **Fallback process**: Re-execute full v×k key cycling on fallback LiteLLM URL
- **If fallback also fails**: Fall through to normal error classification (variant fallback for 429, error return for others)

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by ms_requests_remaining). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked). Independent from RPM.

### /health endpoint — NEVER use on LiteLLM
- LiteLLM /health → per-deployment checks → fd exhaustion. Use /health/liveliness.
- Proxy /health → simple status check → SAFE for Docker healthcheck.

## R27 Hotfix (2026-06-13 01:44 CST)

### Problem: opc2_uname proxy containers crash loop → CC ConnectionRefused stuck

**Root cause chain**:
1. opc2_uname's handlers.py was updated to import `from .codex import handle_codex_responses` (R24 Codex support)
2. But the `codex.py` module file was never created in the gateway directory
3. → Python `ModuleNotFoundError: No module named 'gateway.codex'` on import
4. → proxy container crashes on startup → Docker restart loop (Restarting every ~26 seconds)
5. → CC connects to proxy port → ConnectionRefused (no server listening)
6. → CC stuck: "API Error: Unable to connect to API (ConnectionRefused)"
7. → CC session "Optimize cc 40001-41001 link based on log analysis" dead

**Why fallback didn't work**: The proxy itself was dead — there was no server process to receive requests and trigger any fallback logic. All fallback mechanisms (variant fallback, LiteLLM fallback) operate inside the proxy. If the proxy can't start, none of them can execute.

**Fix**:
1. Created `gateway/codex.py` module implementing:
   - `responses_to_chat_body()` — Responses API request → Chat Completions request conversion
   - `chat_to_responses_result()` — Chat Completions response → Responses API response conversion
   - `convert_stream_chunk_to_responses_event()` — SSE streaming chunk conversion
   - `handle_codex_responses()` — Main handler function (request → upstream → response conversion)
2. Synced R26/R27 changes from remote to local:
   - `UPSTREAM_TIMEOUT=60` (per-key timeout, separated from PROXY_TIMEOUT=300)
   - LiteLLM fallback logic in upstream.py (R26: all-connection-errors → try ms_uni41002)
   - `_cx` agent suffix in AGENT_SUFFIXES and MODEL_MAP
   - `fallback_chat_url` in MODEL_UPSTREAMS config
3. Rebuilt proxy containers on opc2_uname
4. Verified: 4 containers healthy, all 3 format paths working (Anthropic, OpenAI, Responses API)

## Agent Suffix Model IDs (R27)

| Suffix | Agent | Format | Endpoint | Error Cycling | LiteLLM Fallback |
|--------|-------|--------|----------|---------------|-------------------|
| `_cc` | Claude Code | Anthropic→OpenAI conversion | /v1/messages | ✅ 429/500/502/timeout | ✅ R26 |
| `_ol` | OpenClaw | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout | ✅ R26 |
| `_oc` | OpenCode | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout | ✅ R26 |
| `_hm` | Hermes | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout | ✅ R26 |
| `_cx` | Codex | Responses API→Chat Completions conversion | /v1/responses | ✅ 429/500/502/timeout | ✅ R26 |

Frontend model IDs: `glm5.2_cc`, `glm5.2_ol`, `glm5.2_oc`, `glm5.2_hm`, `glm5.2_cx`
Backward compat: `glm5.2` = `glm5.2_cc`, `claude-opus-4-8` = `glm5.2_cc`, `codex-mini-latest` = `glm5.2_cx`

## 10 Variant Model IDs (ms_uni41001, R24 — glm5.2 only)

**GLM-5.2 (ms_uni41001):** 10 variants × 7 keys = 70 deployments
`ZHIPUAI/GLM-5.2`, `ZHIPUAI/GLm-5.2`, `ZHIPUAI/GlM-5.2`, `ZHIPUAI/Glm-5.2`, `ZHIPUAI/gLM-5.2`, `ZHIPUAI/gLm-5.2`, `ZHIPUAI/glM-5.2`, `ZHIPUAI/glm-5.2`, `ZHIPUAi/GLM-5.2`, `ZHIPUAi/GLm-5.2`

**DSv4P — R24 removed entirely** (was 10 variants × 7 keys = 70 dep, all purged from LiteLLM and proxy config)

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**
