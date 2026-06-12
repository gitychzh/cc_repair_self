# Deploy Status — opc_uname + opc2_uname (R24.2, 2026-06-12)

## Architecture (R24.2 — glm5.1 only, single proxy container)
```
                    :40001 proxy gateway (R24.2 multi-agent unified)
                    ├── _cc (Claude Code)  → /v1/messages → Anthropic→OpenAI conversion → upstream.py v×k cycling + variant fallback
                    ├── _ol (OpenClaw)     → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback
                    ├── _oc (OpenCode)     → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback
                    ├── _hm (Hermes)       → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback
                    ├── _cx (Codex CLI)    → /v1/responses → Responses↔Chat Completions conversion → upstream.py v×k cycling + variant fallback [R24.2 NEW]
                    │
                    → :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) [UNIFIED, glm5.1 only]
                    → ModelScope API
```

Proxy does **format conversion (CC only) + variant×key 2D round-robin + variant fallback (R23) + error cycling (429/500/502) + metrics logging** for ALL agent types. OpenAI agents get passthrough (no format conversion) but same error cycling + variant fallback protection. Proxy precisely specifies variant+key combo — LiteLLM does NOT do routing, just forwards.

**Variant×Key 2D Round-Robin + Variant Fallback (R21→R23)**:
- request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- → model name: `glm5.1v{V}k{K}` (e.g. glm5.1v1k1)
- Error cycling (429/500/502): same variant, next key (k→k+1). All 7 keys failed → **R23: try 2 fallback variants (1 key each)** before returning to agent
- Variant fallback (R23): All 7 keys 429 in start variant → try (start_variant+1) and (start_variant+2), each with 1 key attempt. Max extra quota waste = 2 keys per request
- After variant fallback also fails → classify and return to agent (all-429→rate_limit **retry-after=180s**; has-500/502→api_error; has-timeout→502)
- Each variant has independent 200/id/day quota on ModelScope

**R24: All agents route to glm5.1 only**. opus/sonnet/haiku/mini aliases all → glm5.1 (dsv4p removed entirely).

## Containers (R24.2)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM | 7 groups × 10 variants = 70 dep (glm5.1 only), ulimits nofile=2048, memory 1GiB |
| auth_to_api_40001 | :40001 | Proxy (all agents) | R24.2 multi-agent gateway: CC/_cc + OpenClaw/_ol + OpenCode/_oc + Hermes/_hm + Codex/_cx |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine (only litellm_glm51 DB) |

**40002 removed (R24.2)**: 单 proxy 容器处理所有 agent 格式。opc2_uname 本机已同步清理 ✅。

## Deploy Method (R21+)
```bash
# ms_uni41001 config change → restart only
docker restart ms_uni41001

# proxy change → rebuild (need new Dockerfile build)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```

**⚠️ CRITICAL R21 Deploy Order**: ms_uni41001 container must be running with R21 config BEFORE proxy is rebuilt. Proxy sends `glm5.1v1k1` etc to LiteLLM — if LiteLLM doesn't have these model names → "Invalid model name" → CC crash.

**Deploy order for R21 on opc_uname**:
1. Copy new litellm-glm51/config.yaml → /opt/cc-infra/litellm-glm51/config.yaml
2. Copy new docker-compose.yml → /opt/cc-infra/docker-compose.yml
3. Copy new gateway/ package → /opt/cc-infra/proxy/gateway/
4. Start ms_uni41001: `cd /opt/cc-infra && docker compose up -d ms_uni41001`
5. Wait for ms_uni41001 to become healthy: `docker ps` check
6. Rebuild proxy: `cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001`
7. Verify: curl test glm5.1, check /v1/models

**⚠️ opc2_uname NOT YET DEPLOYED** — will only deploy after opc_uname proven stable for ≥2 hours.

**opc_uname R24.4 DEPLOYED 2026-06-12**: 3 containers healthy (40002 removed). Codex CLI end-to-end verified ✅ (exec mode: "echo hello world" → output "hello world"). All agent types (CC/OpenClaw/OpenCode/Hermes/Codex) functional.

## Current Parameters (R24)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Both containers running 3.0 ✅ |
| NUM_KEYS | 7 | docker-compose.yml | Keys per model for round-robin |
| NUM_VARIANTS_GLM51 | 10 | docker-compose.yml | R21: variants per key group for glm5.1 |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Seconds |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (ms_uni41001) | 300 | litellm config.yaml | Seconds |
| num_retries (ms_uni41001) | 0 | litellm config.yaml | R22: proxy handles all error cycling; LiteLLM pure pass-through |
| cooldown_time (ms_uni41001) | 10 | litellm config.yaml | — |
| routing_strategy (ms_uni41001) | simple-shuffle | litellm config.yaml | Proxy specifies exact model, LiteLLM just forwards |
| RateLimitErrorAllowedFails | 0 | litellm config.yaml | R22: 429 cycling by proxy, LiteLLM no retry (avoid wasting quota) |
| TimeoutErrorAllowedFails | 0 | litellm config.yaml | R22: timeout cycling by proxy |
| InternalServerErrorAllowedFails | 0 | litellm config.yaml | R22: 500/choice:null cycling by proxy |
| API_TIMEOUT_MS | 600000 | settings.json | R22: CC→proxy HTTP total timeout (5min→10min) |

## opc2_uname Link Verification (R24.2, 2026-06-12)

**opc2_uname 所有配置与仓库完全一致** ✅：
- gateway module: all 10 files synced (config.py, converters.py, codex.py, error_mapping.py, handlers.py, __init__.py, logger.py, stream.py, upstream.py + proxy.py entry point)
- docker-compose.yml: R24.2 version (3 containers only, 40002 removed, no dsv4p env vars)
- litellm-glm51/config.yaml: R24 version (70 dep glm5.1 only)
- 3个容器全部 healthy (ms_uni41001, cc_postgres, auth_to_api_40001)
- Proxy env vars: NUM_KEYS=7, NUM_VARIANTS_GLM51=10, PROXY_TIMEOUT=300
- CC settings.json: model=glm5.1_cc, API_TIMEOUT_MS=600000 ✅
- curl test glm5.1_cc via 40001 returns 200 ✅
- Codex CLI config: ~/.codex/config.toml → base_url=opc_uname:40001, model=glm5.1_cx, wire_api=responses ✅

**R24.2: OpenAI agent 配置修复（opc2_uname 2026-06-12）**

⚠️ 根因: OpenClaw/Hermes/OpenCode 直连 LiteLLM 41001，发送 `model=glm5.1` → LiteLLM 没有 `glm5.1` 别名（只有 v×k 路由名），返回 400 "Invalid model name"

修复: 所有 OpenAI agent 改为通过 proxy gateway (40001) 路由：
- OpenClaw: baseUrl 从 `41001` → `40001`, model 从 `glm5.1` → `glm5.1_ol`
- Hermes: base_url 从 `41001` → `40001`, default 从 `glm5.1` → `glm5.1_hm`, fallback 为空（40002 已删除）
- OpenCode: baseURL 从 `41001` → `40001`, model 从 `glm5.1` → `glm5.1_oc`

⚠️ R24.2: opc2_uname 本机 compose 已同步到仓库版本，40002 容器已删除并清理（3 容器运行）✅

验证结果 ✅：
- curl test glm5.1_ol via 40001 returns 200 ✅
- curl test glm5.1_hm via 40001 returns 200 ✅
- curl test glm5.1_oc via 40001 returns 200 ✅
- Streaming passthrough 40001 _ol returns SSE chunks ✅
- contextWindow 从 131072 → 170000（与 proxy /v1/models 一致）

**opc_uname 可达 via tailscale**: SSH `opc2sname-tailscale:222` ✅

## Log System Analysis (R22, 2026-06-12)

### Proxy日志（3层日志系统）

| 日志层 | 文件格式 | 内容 | 大小趋势 |
|--------|----------|------|----------|
| proxy.{date}.log | 纯文本 | 每请求一行简要日志（REQ/ERR/TIMEOUT等） | 0.2-0.6MB/天 |
| metrics.{date}.jsonl | JSON行 | 结构化metrics：request_id, model, ttfb_ms, tokens, variant_idx, key_idx | 0.2-2.5MB/天 |
| error_detail.{date}.jsonl | JSON行 | 详细错误：error_subcategory, upstream_error_body, key_cycle_attempts | 0-0.35MB/天 |

**proxy 40001 logs**: 12MB总计（10天有数据，06-06/07/08空缺=proxy重建期间）
**proxy 40002 logs**: 已删除（R24.2 清理）

### Docker容器日志

- json-file driver: max-size=50m, max-file=5, tag=container/{{.Name}}
- 自动rotation ✅，每个容器最多250MB

### LiteLLM日志

- `/opt/cc-infra/logs/litellm-glm51/`: 空目录（LiteLLM日志写入容器内/app/logs/，volume挂载但无日志文件）
- **问题**: LiteLLM litellm_settings.json_logs=true，但日志文件未出现在挂载目录中。可能是LiteLLM写入PostgreSQL而非文件。

### ⚠️ 缺失：Proxy日志无自动清理机制

- proxy.py按日期写文件（proxy.{date}.log, metrics.{date}.jsonl, error_detail.{date}.jsonl）
- **无rotation/purge/cleanup**: 日志文件永不删除
- Docker容器日志有50m×5 rotation ✅，但proxy自己的应用日志无任何清理
- **建议**: 添加crontab任务，保留最近7天proxy日志，删除7天前的日志文件
- 当前影响: 12MB/10天 ≈ 1.2MB/天，6个月约216MB。不紧急但需关注。

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality

### Variant×Key 2D Round-Robin (R21) — CRITICAL deploy order
- **ms_uni41001 MUST be running first**: Proxy sends `glm5.1v1k1` to LiteLLM. If LiteLLM doesn't have these names → "Invalid model name" → CC crash
- **Deploy order**: 1) ms_uni41001 config + start → 2) Verify LiteLLM has v+k model names → 3) Proxy rebuild → 4) Verify proxy /v1/models only shows canonical names

### Single point of failure (R24)
- All agents route to the same ms_uni41001 container (glm5.1 only)
- If ms_uni41001 crashes → ALL models unavailable
- **Mitigation**: ms_uni41001 has been stable since R21 deploy. If it fails, proxy env vars can be changed to route to a new LiteLLM container on any port.

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by ms_requests_remaining). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked). Independent from RPM.

### /health endpoint — NEVER use on LiteLLM
- LiteLLM /health → per-deployment checks → fd exhaustion. Use /health/liveliness.
- Proxy /health → simple status check → SAFE for Docker healthcheck.

### 5. Variant fallback + retry-after=180s (opc2_uname push, R23)
- **Issue**: 5 CC processes consuming quota simultaneously → ALL-KEYS-429 for glm5.1 → CC stuck in 429 retry loop
- **Root cause**: Token quota per-key shared across all variants. 7 key cycling wastes 7 quota per attempt. 5 CC processes × 429 retry loop = massive quota waste
- **Immediate fix**: Kill 3 redundant CC processes. Quota recovered in ~8 minutes (17:40→17:48)
- **Prevention fix**: Proxy now tries 2 additional variants (1 key each) when all 7 keys in start variant are 429. Max extra waste = 2 keys (vs 7×N from CC retry loop). retry-after changed from 30s to 180s
- **New log labels**: VARIANT-FALLBACK-START, VARIANT-FALLBACK-TRY, VARIANT-FALLBACK-429, VARIANT-FALLBACK-SUCCESS, VARIANT-FALLBACK-ERR, VARIANT-FALLBACK-TIMEOUT, VARIANT-FALLBACK-CONNERR, VARIANT-FALLBACK-ALL-FAILED
- **New metrics fields**: variant_fallback, fallback_variant_idx, fallback_key_idx, variant_fallback_attempts, variant_fallback_429s_before_success, variant_fallback_all_failed
- **Deployed on opc_uname**: 2026-06-12 18:13 CST. Both proxy containers rebuilt and healthy. Curl test glm5.1+dsv4p return 200 ✅

## R23 Changes (2026-06-12)

### 1. R21 gateway code deployed to opc_uname container (opc_uname push)
- **Issue**: Remote opc_uname container was running R19 gateway code (key-only round-robin, `glm5.1k1~k7` format)
- **Root cause**: Docker container image was stale — R21 code was on disk but container wasn't rebuilt
- **Fix**: `docker compose up -d --build --force-recreate auth_to_api_40001`
- **Verified**: Logs show `v{V}k{K}` format, NUM_VARIANTS={glm5.1:10, dsv4p:10}

### 2. PROXY_TIMEOUT=2s timeout cycling test (opc_uname push)
- **Purpose**: Verify that socket.timeout correctly triggers key cycling (R21 feature)
- **Test**: Changed PROXY_TIMEOUT from 300s to 2s, observed gateway logs for ~10 minutes
- **Results**: ✅ All key cycling features verified (socket.timeout captured, k→k+1 cycling, 502 api_error to CC)
- **PROXY_TIMEOUT restored to 300s** after test completed

### 3. SSH connection updated (opc_uname push)
- **opc_uname SSH**: Changed from `192.168.1.104:222` to `100.109.57.26:222` (tailscale IP)

### 4. Removed 41003/42001 containers (opc2_uname push)
- **These containers were retained but NOT routed since R21** — no traffic went through them
- ms_uni41001 handles all glm5.1 + dsv4p traffic as sole upstream
- **Removed from docker-compose.yml**: 6 containers → 3 containers (cc_postgres, ms_uni41001, auth_to_api_40001)
- **Deleted config files**: `configs/litellm-glm51-test/config.yaml` (839 lines), `configs/litellm-dsv4p/config.yaml` (923 lines)
- **Removed PostgreSQL databases**: litellm_glm51_test and litellm_dsv4p from POSTGRES_MULTIPLE_DATABASES
- **Updated scripts/docs**: Removed all 41003/42001 references

## R22 Changes (2026-06-12)

### 1. Proxy error cycling expanded: 429 → 429+500+502
- **ModelScope deducts quota for every request**, even errors (429, choice:null/500, 502)
- **New behavior**: 429, 500, 502 all cycle to next key (same variant, k→k+1)
- **All-keys-exhausted classification**: all-429→rate_limit_error; has-500→api_error; has-502→api_error; has-timeout→502 api_error
- **Not cycling**: 400 input overflow, 400 inappropriate content, 400 thinking_budget, 401/403 auth errors

### 2. LiteLLM num_retries=0, all allowed_fails=0
- **R21 architecture**: each model_name has exactly 1 deployment → LiteLLM has NO fallback to try
- **New values**: all 0 → LiteLLM is pure pass-through, proxy handles all cycling

### 3. CC API_TIMEOUT_MS: 300000→600000 (5min→10min)
- **CC SDK default**: 600000ms (10 min) — we now match it

**R22 DEPLOYED on opc_uname 2026-06-12 15:20 CST**: Proxy rebuilt + LiteLLM restarted + CC settings updated.

## R21 Changes (2026-06-12)

### 1. Unified container ms_uni41001 (PRIMARY change)
- **14 key groups**: 7 glm5.1 groups (k1~k7 × v1~v10 = 70 dep) + 7 dsv4p groups (k1~k7 × v1~v10 = 70 dep)
- **model_name format**: `{base}v{V}k{K}` (e.g. glm5.1v1k1, dsv4pv3k5)
- **dsv4p reduced from 11→10 variants**: Removed `deepseek-ai/DeEpSeek-V4-Pro` per user decision

### 2. Proxy variant×key 2D round-robin (R21)
- **2D counter**: request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- **Error cycling (429/500/502)**: same variant, cycle to next key (k→k+1)

### 3. All routing → ms_uni41001 (single upstream)
- Both glm5.1 and dsv4p now route to ms_uni41001

### 4. Resource adjustment for ms_uni41001
- nofile: 8192→2048 (140 dep vs old 7000)
- memory: 2GiB→1GiB
- CPU: 2→1
- start_period: 180→60s

## Parameter Change History (condensed)

| Round | Changes | Result |
|-------|---------|--------|
| R1-5 | cooldown params, socket bug, conn_retry removal, num_retries=5 | 85.4%→100% |
| R12 | Removed proxy auto-compact; safety 120K→170K; contextWindow 120K→170K | 80%→97% |
| R7 | CHARS_PER_TOKEN 2.0→3.0; safety 170K→190K; contextWindow 170K→190K | 99.6% |
| R15 | compactWindow 180K→140K; contextWindow/safety 190K→170K | 99.8% |
| R16 | compactWindow 140K→155K (CC overestimation 1.7x) | 99.8% best ever |
| R17 | opc2_uname full sync: num_retries 30→8 + settings.json 155K + proxy.py parity | 99.8%+ stable |
| R18 | Tier-based routing + THINKING_SUPPORT + haiku→dsv4p + gateway package | 100% success |
| R18.1-3 | Metrics analysis; dsv4p memory 2GiB; glm5.1_uni41001 memory 2GiB | OOM prevented ✅ |
| R19 | Key round-robin (7 groups per model, 429 cycling); num_retries 8→2/5→2 | Key cycling ✅ |
| R19.1 | socket.timeout单独捕获 + timeout_exceeded_by_ms + 全key失败分类 | No timeout events yet |
| R20 | 41003 variant reduction 1000→10; resource savings | Deploying, verified ✅ |
| R21 | Unified ms_uni41001 (140 dep glm5.1+dsv4p); variant×key 2D round-robin; dsv4p 11→10 variants | DEPLOYED ✅ |
| R22 | Proxy 429+500+502 error cycling; LiteLLM num_retries=0 all allowed_fails=0; CC API_TIMEOUT_MS 600000 | DEPLOYED ✅ |
| R23 | opc_uname: R21 gateway deployed+timeout cycling test ✅+variant fallback+retry-after=180s; opc2_uname: removed 41003/42001 containers | Config cleanup + gateway verified ✅ |
| R23.1 | Multi-agent gateway refactoring: upstream.py shared module, AGENT_SUFFIXES (_cc/_ol/_oc/_hm), OpenAI error format, _handle_openai_with_cycling() | **DEPLOYED 2026-06-12 18:20 CST; CC/OpenClaw/OpenCode/Hermes all verified ✅** |
| R24.2 | Codex _cx Responses API support + 40002 container removed + proxy.py monolith deleted (2217 lines → gateway/ package); codex.py bidirectional conversion | **DEPLOYED 2026-06-12; CC/OpenClaw/OpenCode/Hermes/Codex all verified ✅** |
| R24.3 | Fix Codex streaming: merge reasoning_content+content into output_text delta (GLM-5.1 sends both in same delta chunk, content="" during reasoning was falsy → 0 text) | **DEPLOYED ✅; streaming delta events now include reasoning** |
| R24.4 | Fix Codex tools: filter function(no-name) + non-function types; Codex CLI 0.134.0 sends 10 tools without name property → LiteLLM 400; skip tool_choice if all tools skipped | **DEPLOYED ✅; Codex CLI end-to-end verified: "echo hello world" → output "hello world"** |

## 10 Variant Model IDs (ms_uni41001, R24 — glm5.1 only)

**GLM-5.1 (ms_uni41001):** 10 variants × 7 keys = 70 deployments
`ZHIPUAI/GLM-5.1`, `ZHIPUAI/GLm-5.1`, `ZHIPUAI/GlM-5.1`, `ZHIPUAI/Glm-5.1`, `ZHIPUAI/gLM-5.1`, `ZHIPUAI/gLm-5.1`, `ZHIPUAI/glM-5.1`, `ZHIPUAI/glm-5.1`, `ZHIPUAi/GLM-5.1`, `ZHIPUAi/GLm-5.1`

**DSv4P — R24 removed entirely** (was 10 variants × 7 keys = 70 dep, all purged from LiteLLM and proxy config)

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**

## R23.1: Multi-Agent Gateway Refactoring (2026-06-12)

### Changes

**Module Refactoring (low coupling, high cohesion):**
- **NEW** `gateway/upstream.py` — shared v×k cycling + error handling module used by ALL agent types
  - `UpstreamResult` class: unified result from upstream executor
  - `execute_request()` → UpstreamResult (extracted all v×k round-robin, 429/500/502 key cycling, timeout cycling, thinking_budget fix retry, variant fallback logic)

- **MODIFIED** `gateway/config.py` — agent suffix system
  - `AGENT_SUFFIXES` dict: _cc→anthropic, _ol/_oc/_hm→openai
  - `detect_agent_type(model_id)` → (base_model, agent_suffix, response_format)
  - Updated MODEL_MAP with all suffix entries

- **MODIFIED** `gateway/handlers.py` — slim dispatcher using upstream.py
  - `_handle_messages()` → Anthropic path for CC/_cc
  - `_handle_openai_with_cycling()` → OpenAI path for _ol/_oc/_hm
  - `_stream_openai_passthrough()` → SSE passthrough (byte-level, metrics extraction)
  - Force-stream-for-nonstream ONLY for Anthropic path (CC), NOT for OpenAI agents

- **MODIFIED** `gateway/error_mapping.py` — OpenAI error format handlers
  - `format_openai_error_all_keys_exhausted()` + `format_openai_error_upstream()`

### Agent Suffix Model IDs

| Suffix | Agent | Format | Endpoint | Error Cycling |
|--------|-------|--------|----------|---------------|
| `_cc` | Claude Code | Anthropic→OpenAI conversion | /v1/messages | ✅ 429/500/502/timeout |
| `_ol` | OpenClaw | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout |
| `_oc` | OpenCode | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout |
| `_hm` | Hermes | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout |
| `_cx` | Codex CLI | Responses↔Chat Completions conversion | /v1/responses | ✅ 429/500/502/timeout |

Frontend model IDs: `glm5.1_cc`, `glm5.1_ol`, `glm5.1_oc`, `glm5.1_hm`, `glm5.1_cx`
Backward compat: `glm5.1` = `glm5.1_cc`, `claude-opus-4-8` = `glm5.1_cc`

### Test Results

- **CC (_cc):** ✅ streaming, ✅ non-stream, ✅ backward compat, ✅ 429 key cycling
- **OpenClaw (_ol):** ✅ streaming passthrough, ✅ non-stream, ✅ 429 error (OpenAI format), ✅ 500 key cycling
- **OpenCode (_oc):** ✅ streaming, ✅ non-stream, ✅ agent=_oc detected
- **Hermes (_hm):** ✅ non-stream, ✅ agent=_hm detected, ✅ 429 error (OpenAI format)

### R24: dsv4p Purge + Container Cleanup (2026-06-12)

1. **CC settings.json model → glm5.1_cc** (both machines) ✅
2. **opc2_uname R23.1 gateway synced** ✅
3. **41003/42001 containers removed** ✅ (docker stop+rm on both machines)
4. **Config dirs + logs cleaned** ✅ (litellm-glm51-test, litellm-dsv4p, gateway_old_R22, old backups)
5. **PostgreSQL databases dropped** ✅ (litellm_glm51_test, litellm_dsv4p)
6. **dsv4p removed from LiteLLM config** ✅ (140 dep → 70 dep glm5.1 only)
7. **dsv4p removed from proxy config** ✅ (MODEL_UPSTREAMS, MODEL_MAP, VARIANT_IDS, THINKING_SUPPORT, etc.)
8. **dsv4p env vars removed from docker-compose.yml** ✅ (LITELLM_URL_DSV4P, MODEL_INPUT_TOKEN_SAFETY_DSV4P, NUM_VARIANTS_DSV4P)
9. **All aliases now → glm5.1** ✅ (haiku/mini/tier previously → dsv4p, now all → glm5.1)
10. **Both machines rebuilt + verified** ✅ (3 containers healthy, curl glm5.1_cc returns 200)

### R24.2: Codex _cx Responses API + Container Merge (2026-06-12)

**Architecture change**: 40002 proxy container removed entirely. Single 40001 container now handles ALL 5 agent formats.

1. **NEW `gateway/codex.py`** (~900 lines) — Responses API ↔ Chat Completions bidirectional conversion module
   - `responses_to_chat_body()` — Converts Codex Responses API request → Chat Completions request
     - Maps `instructions` → system message, `input` → messages array (string or array)
     - Maps `function` type tools → Chat Completions tools format
     - Maps `max_output_tokens` → `max_completion_tokens`
   - `chat_to_responses()` — Converts Chat Completions response → Responses API response
     - Creates `output[]` array with `message` (output_text) + `function_call` items
     - Maps tool_calls → separate function_call items with `call_id`
   - `stream_responses_passthrough()` — Converts Chat Completions SSE → Responses API named SSE events
     - Emits: response.created, response.in_progress, response.output_item.added, response.content_part.added, response.output_text.delta, response.function_call_arguments.delta, etc.
   - `handle_codex_responses()` — End-to-end handler (format convert → force-stream → execute_request → dispatch)
   - `_collect_stream_to_responses()` — Collect forced stream → synthesize non-stream Responses API response

2. **MODIFIED `gateway/config.py`** — Added `_cx` suffix
   - `AGENT_SUFFIXES["_cx"] = {"name": "Codex", "format": "responses"}`
   - `MODEL_MAP["glm5.1_cx"] = "glm5.1"` (no dsv4p_cx — dsv4p removed in R24)

3. **MODIFIED `gateway/handlers.py`** — Added /v1/responses route
   - `do_POST()` and `do_HEAD()` now handle `/v1/responses`
   - `_handle_codex_responses()` → detect agent type → map model → delegate to codex module

4. **MODIFIED `gateway/error_mapping.py`** — Responses API error formats
   - `format_responses_error_all_keys_exhausted()` — flat format: `{"error": {"type", "code", "message"}}`
   - `format_responses_error_upstream()` — upstream error in Responses API format

5. **DELETED `proxy/proxy.py`** (2217 lines) — Old monolithic proxy, replaced by gateway/ package in R23.1

6. **MODIFIED `docker-compose.yml`** — 40002 container removed
   - Single proxy container (auth_to_api_40001) handles all formats
   - 3 containers: cc_postgres, ms_uni41001, auth_to_api_40001

7. **Bug fixes (R24.1 + R24.2 + R24.3 + R24.4)**:
   - Usage extraction: `chunk_data.get("completion_tokens")` → `chunk_usage.get("completion_tokens")`
   - stream_options for force-stream: Added `stream_options={"include_usage": True}` after `oai_body["stream"] = True` (was missing for non-stream Responses API requests → usage=0)
   - **R24.3: Streaming reasoning merge** — GLM-5.1 sends `reasoning_content` and `content` in the same delta chunk. During reasoning, `content=""` was falsy in `if text_delta:` check, causing ALL output_text.delta events to be skipped (201 tokens generated but 0 text in stream). Fixed by merging `reasoning_content + content` into `merged_delta` for Codex `output_text` events. Responses API has no separate reasoning output type — Codex needs full model output as `output_text`. Same fix applied to `_collect_stream_to_responses()` for non-stream mode.
   - **R24.4: Codex tools filtering** — Codex CLI 0.134.0 sends 10 internal tools (shell etc.) as `function` type WITHOUT `name` property → LiteLLM 400 `'name' is a required property`. Fixed by filtering out function tools without name, skipping non-function types (web_search, file_search, code_interpreter, etc. — not supported by ModelScope). If all tools skipped, also skip `tool_choice`.

### Test Results (R24.4 — FINAL)

- **CC (_cc):** ✅ /v1/messages returns 200, streaming + non-stream work
- **OpenClaw/_ol, OpenCode/_oc, Hermes/_hm:** ✅ all passthrough formats verified
- **Codex (_cx):** ✅ /v1/responses non-stream returns 200 with reasoning+content merged in output_text
- **Codex (_cx):** ✅ /v1/responses streaming returns SSE with named events + reasoning+content merged delta
- **Codex CLI:** ✅ end-to-end verified — `codex exec "echo hello world"` → output "hello world"
- **Codex CLI:** ✅ `codex exec "what is 2+2"` → output "4"
- **40002 container:** ✅ removed (docker rm + --remove-orphans)
- **proxy.py:** ✅ deleted from repo (replaced by gateway/ package)
