# Deploy Status — opc_uname + opc2_uname (R28, 2026-06-13)

## Architecture (R28 — glm5.1 only, dual proxy + dual LiteLLM)
```
                    :40001 proxy gateway (R24.4 multi-agent unified, PRIMARY)
                    ├── _cc (Claude Code)  → /v1/messages → Anthropic→OpenAI conversion → upstream.py v×k cycling + variant fallback + LiteLLM fallback
                    ├── _ol (OpenClaw)     → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback + LiteLLM fallback
                    ├── _oc (OpenCode)     → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback + LiteLLM fallback
                    ├── _hm (Hermes)       → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback + LiteLLM fallback
                    ├── _cx (Codex CLI)    → /v1/responses → Responses↔Chat Completions conversion → upstream.py v×k cycling + variant fallback + LiteLLM fallback
                    │
                    :40002 proxy gateway (R25 FALLBACK — identical config)
                    ├── Same agent types, same format conversion, same v×k cycling, same LiteLLM fallback
                    ├── Agents fallback to 40002 when 40001 is restarting/unavailable
                    │
                    → :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) [PRIMARY]
                    → :41002 LiteLLM ms_uni41002 (glm5.1v1k1~v10k7 = 70 dep) [FALLBACK — identical config, same keys]
                    → ModelScope API
```

Proxy does **format conversion (CC only) + variant×key 2D round-robin + variant fallback (R23) + LiteLLM fallback (R26) + error cycling (429/500/502) + metrics logging** for ALL agent types. OpenAI agents get passthrough (no format conversion) but same error cycling + variant fallback + LiteLLM fallback protection. Proxy precisely specifies variant+key combo — LiteLLM does NOT do routing, just forwards.

**R26: Dual LiteLLM (41001 primary + 41002 fallback)**:
- Both LiteLLM containers share the same config.yaml (70 dep, same model_list)
- Both use the same 7 MS_KEYs → same ModelScope quota (quota is per-key, not per-container)
- 41002 uses independent PostgreSQL DB (litellm_glm51_fallback) to avoid lock contention
- Proxy auto-switches to 41002 when 41001 container is unavailable (ConnectionRefused/ConnectionError/SocketTimeout)
- LiteLLM fallback only triggers on **connection errors** — 429/500/502 are ModelScope issues (same keys = same quota, switching LiteLLM container won't help)
- Both 40001 and 40002 proxy have the same LiteLLM fallback configuration

**R25: Dual proxy (40001 primary + 40002 fallback)**:
- When 40001 proxy restarts/rebuilds, agents automatically fallback to 40002
- 40002 is an identical proxy — same config, same Dockerfile, same gateway code, only different port

**Variant×Key 2D Round-Robin + Variant Fallback (R21→R23)**:
- request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- → model name: `glm5.1v{V}k{K}` (e.g. glm5.1v1k1)
- Error cycling (429/500/502): same variant, next key (k→k+1). All 7 keys failed → **R23: try 2 fallback variants (1 key each)** before returning to agent
- Variant fallback also fails → classify and return: all-429→rate_limit **retry-after=180s**; has-500/502→api_error; has-timeout→502

## Containers (R28)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | LiteLLM (PRIMARY) | 7 groups × 10 variants = 70 dep (glm5.1 only), ulimits nofile=2048, memory 1GiB, DB=litellm_glm51 |
| ms_uni41002 | :41002 | LiteLLM (FALLBACK) | R26: identical to 41001, same config.yaml, same 7 MS_KEYs, DB=litellm_glm51_fallback |
| auth_to_api_40001 | :40001 | Proxy (all agents, PRIMARY) | R28: multi-agent gateway + LiteLLM fallback (41001→41002) + UPSTREAM_TIMEOUT=60s |
| auth_to_api_40002 | :40002 | Proxy (all agents, FALLBACK) | R28: identical to 40001, LiteLLM fallback (41001→41002) + UPSTREAM_TIMEOUT=60s |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine (litellm_glm51 + litellm_glm51_fallback DBs) |

## LiteLLM Fallback (R26)
| Trigger | Behavior | Reason |
|---------|----------|--------|
| All keys ConnectionRefused/ConnectionError | Auto-switch to ms_uni41002, retry same (variant_idx, key_idx) | Primary LiteLLM container down (restart/crash) |
| All keys SocketTimeout | Auto-switch to ms_uni41002, retry same (variant_idx, key_idx) | Primary LiteLLM container unreachable |
| 429/500/502 on any key | Normal key cycling (same LiteLLM container) | ModelScope quota/server issues — same keys on 41002 = same result |
| Mixed errors (429 + connection) | Normal cycling + variant fallback, NO LiteLLM fallback | LiteLLM is alive (429 proves it), just quota exhausted |
| Fallback LiteLLM also fails | Return all_keys_exhausted error to agent | Both LiteLLM containers unavailable |

## Agent Fallback Mechanism (R25+R26)
| Agent | Proxy Fallback | LiteLLM Fallback | Config |
|-------|---------------|-----------------|--------|
| Claude Code (_cc) | restart_claude.sh health check (40001→40002) | Proxy auto (41001→41002 on conn err) | ANTHROPIC_BASE_URL |
| Codex CLI (_cx) | Manual model_provider switch | Proxy auto | codex config.toml |
| OpenClaw (_ol) | Native fallback model (litellm-fallback/glm5.1_ol via 40002) | Proxy auto | openclaw.json |
| Hermes (_hm) | Native fallback_providers (proxy-gateway-fallback via 40002) | Proxy auto | hermes.yaml |
| OpenCode (_oc) | Multiple providers (proxy-gateway-fallback via 40002) | Proxy auto | opencode.jsonc |

## Deploy Method
```bash
# ms_uni41001 config change → restart only
docker restart ms_uni41001

# ms_uni41002 config change → restart only (shares same config.yaml file)
docker restart ms_uni41002

# Both LiteLLM containers
docker restart ms_uni41001 ms_uni41002

# proxy change → rebuild both proxies (they share same Dockerfile/code)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002

# Only rebuild 40001
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001

# Only rebuild 40002
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40002

# Full rebuild (all 5 containers)
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart (with 40001→40002 auto-fallback)
bash ~/cc_ps/cc_recover/restart_claude.sh
```

**⚠️ CRITICAL Deploy Order**: LiteLLM containers must be running BEFORE proxy is rebuilt. Proxy sends `glm5.1v1k1` etc to LiteLLM — if LiteLLM doesn't have these model names → "Invalid model name" → CC crash.

**Deploy order**:
1. Copy litellm-glm51/config.yaml → /opt/cc-infra/
2. Copy docker-compose.yml → /opt/cc-infra/
3. Copy gateway/ package → /opt/cc-infra/proxy/gateway/
4. Start ms_uni41001: `cd /opt/cc-infra && docker compose up -d ms_uni41001`
5. Start ms_uni41002: `cd /opt/cc-infra && docker compose up -d ms_uni41002`
6. Wait for both LiteLLM healthy: `docker ps` check
7. Rebuild proxy 40001: `cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001`
8. Rebuild proxy 40002: `cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40002`
9. Verify: curl test glm5.1 via 40001, 40002, and direct 41001, 41002

## Current Parameters (R28)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Proxy overestimates 1.36x (chars_json/3.0 vs actual) |
| NUM_KEYS | 7 | docker-compose.yml | Keys per model for round-robin |
| NUM_VARIANTS_GLM51 | 10 | docker-compose.yml | R21: variants per key group |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Overall request timeout concept (for docs), NOT used as HTTPConnection timeout |
| UPSTREAM_TIMEOUT | 60 | docker-compose.yml | R27: Per-key HTTPConnection timeout. P99 TTFB=52s, P99 litellm_dur=36s, max litellm=91s → 60s covers 99.7%+ |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (ms_uni41001/41002) | 300 | litellm config.yaml | Seconds |
| num_retries (ms_uni41001/41002) | 0 | litellm config.yaml | Proxy handles all error cycling |
| cooldown_time (ms_uni41001/41002) | 10 | litellm config.yaml | — |
| routing_strategy (ms_uni41001/41002) | simple-shuffle | litellm config.yaml | Proxy specifies exact model |
| All allowed_fails | 0 | litellm config.yaml | LiteLLM pure pass-through |
| API_TIMEOUT_MS | 600000 | settings.json | CC→proxy HTTP total timeout (10min) |

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality

### R26: LiteLLM fallback (connection errors only)
- When ms_uni41001 container is unavailable (restart/crash), proxy auto-switches to ms_uni41002
- Only triggers on **pure connection errors** (ConnectionRefused, ConnectionError, SocketTimeout)
- Does NOT trigger on 429/500/502 — those are ModelScope issues (same keys = same quota)
- If some keys are 429 and some are connection errors → LiteLLM is alive, just cycling through quota
- Fallback LiteLLM uses same keys → quota is shared across both containers (quota is per-key on ModelScope)
- Both 40001 and 40002 proxy have the same LiteLLM fallback (41001→41002) configuration
- Extra memory: ~1GB for ms_uni41002 container

### R25: Dual proxy fallback
- When 40001 proxy is being rebuilt/restarted, agents can use 40002 as fallback
- Both proxies share the same LiteLLM backend (41001 primary + 41002 fallback) — 40002 adds only ~256MB

### Variant fallback + retry-after=180s (R23)
- All 7 keys 429 → try 2 extra variants (1 key each) → max extra waste = 2 keys per request
- retry-after=180s (3 min) — prevents CC 30s retry loop wasting quota
- Fallback also fails → classify: all-429→rate_limit; has-500/502→api_error; has-timeout→502

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by ms_requests_remaining)
- **Token quota**: Per-key hourly/daily (NOT tracked) — independent from RPM

### /health endpoint — NEVER use on LiteLLM
- LiteLLM /health → per-deployment checks → fd exhaustion. Use /health/liveliness.
- Proxy /health → simple status check → SAFE for Docker healthcheck.

### OpenAI agents must route through proxy
- _ol/_oc/_hm cannot connect directly to LiteLLM 41001/41002 (no `glm5.1` alias, only v×k names)
- Direct connection → 400 "Invalid model name"

## 10 Variant Model IDs (ms_uni41001 + ms_uni41002, glm5.1 only)

`ZHIPUAI/GLM-5.1`, `ZHIPUAI/GLm-5.1`, `ZHIPUAI/GlM-5.1`, `ZHIPUAI/Glm-5.1`, `ZHIPUAI/gLM-5.1`, `ZHIPUAI/gLm-5.1`, `ZHIPUAI/glM-5.1`, `ZHIPUAI/glm-5.1`, `ZHIPUAi/GLM-5.1`, `ZHIPUAi/GLm-5.1`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**

## Agent Suffix Model IDs

| Suffix | Agent | Format | Endpoint | Error Cycling | LiteLLM Fallback (R26) | Proxy Fallback (R25) |
|--------|-------|--------|----------|---------------|------------------------|---------------------|
| `_cc` | Claude Code | Anthropic→OpenAI conversion | /v1/messages | ✅ 429/500/502/timeout | ✅ conn err → 41002 | restart_claude.sh auto-detect |
| `_ol` | OpenClaw | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout | ✅ conn err → 41002 | fallback model via 40002 |
| `_oc` | OpenCode | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout | ✅ conn err → 41002 | fallback provider via 40002 |
| `_hm` | Hermes | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout | ✅ conn err → 41002 | fallback_providers via 40002 |
| `_cx` | Codex CLI | Responses↔Chat Completions | /v1/responses | ✅ 429/500/502/timeout | ✅ conn err → 41002 | manual switch in config.toml |

Frontend model IDs: `glm5.1_cc`, `glm5.1_ol`, `glm5.1_oc`, `glm5.1_hm`, `glm5.1_cx`
Backward compat: `glm5.1` = `glm5.1_cc`, `claude-opus-4-8` = `glm5.1_cc`

## opc2_uname Verification ✅ (R28)
- gateway module: all files synced (including error_mapping.py with Responses API format functions)
- docker-compose.yml: R28 version (5 containers including ms_uni41002 + LiteLLM fallback + UPSTREAM_TIMEOUT=60)
- litellm config: 70 dep glm5.1 only (shared by both 41001 and 41002)
- 5 containers healthy
- CC settings.json: model=glm5.1_cc, API_TIMEOUT_MS=600000 ✅
- curl test glm5.1_cc via 40001 returns 200 ✅
- curl test glm5.1_cc via 40002 returns 200 ✅ (R25)
- curl test glm5.1_ol via 40001 returns 200 ✅ (R28)
- curl test glm5.1_cx via 40001 returns 200 ✅ (R28)
- LiteLLM fallback logic: upstream.py includes R26 connection error fallback ✅
- UPSTREAM_TIMEOUT=60s verified in both proxy containers ✅ (R27)
- Metrics now include key_idx, variant_idx, litellm_model on success ✅ (R28)
- Codex CLI end-to-end verified ✅

## opc_uname R28 DEPLOYED ✅
- Codex CLI end-to-end verified (exec mode: "echo hello world" → output "hello world")
- All 5 agent types (CC/OpenClaw/OpenCode/Hermes/Codex) functional

## R27→R28 Change Log

### R27: UPSTREAM_TIMEOUT separation
- **WHY**: PROXY_TIMEOUT=300 was being used as HTTPConnection timeout for each key attempt → 5min per key → 7×300=35min worst case if ModelScope unavailable. P99 data: TTFB=52s, litellm_dur=36s, max litellm=91s → 60s covers 99.7%+ of normal requests.
- **WHAT**: Added UPSTREAM_TIMEOUT env var (default 60s) for per-key HTTPConnection timeout. PROXY_TIMEOUT=300 kept as overall timeout concept (docs reference only). All `timeout=PROXY_TIMEOUT` in `_make_upstream_conn`, `stream.py`, `upstream.py` changed to `timeout=UPSTREAM_TIMEOUT`.
- **DATA**: P99 TTFB=52s, P99 litellm_dur=36s, max litellm=91s → UPSTREAM_TIMEOUT=60 covers 99.7%+ with safety margin.
- **FILES**: config.py, handlers.py, upstream.py, stream.py, app.py, docker-compose.yml

### R28: Success-path metrics logging
- **WHY**: All success paths in upstream.py lacked `_log_metrics()` calls → handlers recorded basic success metrics but missing upstream detail (key_idx, variant_idx, litellm_model, key_cycle_details). Only error path had `_log_metrics`.
- **WHAT**: Added `_log_metrics(metrics)` to key cycling and fallback success paths in upstream.py. Merged upstream result info (key_idx, variant_idx, litellm_model, key_cycle_attempts) into handler metrics in handlers.py and codex.py success paths.
- **BUG FIX**: error_mapping.py was not synced to local /opt/cc-infra/ → missing format_responses_error_all_keys_exhausted → 40002 container crash loop. Fixed by syncing all gateway package files including error_mapping.py, __init__.py, codex.py.
- **FILES**: upstream.py, handlers.py, codex.py, error_mapping.py (sync), __init__.py (sync)
