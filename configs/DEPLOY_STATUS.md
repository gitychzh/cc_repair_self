# Deploy Status — opc_uname + opc2_uname (R24.4, 2026-06-12)

## Architecture (R24.4 — glm5.1 only, single proxy container)
```
                    :40001 proxy gateway (R24.4 multi-agent unified)
                    ├── _cc (Claude Code)  → /v1/messages → Anthropic→OpenAI conversion → upstream.py v×k cycling + variant fallback
                    ├── _ol (OpenClaw)     → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback
                    ├── _oc (OpenCode)     → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback
                    ├── _hm (Hermes)       → /v1/chat/completions → OpenAI passthrough → upstream.py v×k cycling + variant fallback
                    ├── _cx (Codex CLI)    → /v1/responses → Responses↔Chat Completions conversion → upstream.py v×k cycling + variant fallback
                    │
                    → :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) [UNIFIED, glm5.1 only]
                    → ModelScope API
```

Proxy does **format conversion (CC only) + variant×key 2D round-robin + variant fallback (R23) + error cycling (429/500/502) + metrics logging** for ALL agent types. OpenAI agents get passthrough (no format conversion) but same error cycling + variant fallback protection. Proxy precisely specifies variant+key combo — LiteLLM does NOT do routing, just forwards.

**Variant×Key 2D Round-Robin + Variant Fallback (R21→R23)**:
- request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- → model name: `glm5.1v{V}k{K}` (e.g. glm5.1v1k1)
- Error cycling (429/500/502): same variant, next key (k→k+1). All 7 keys failed → **R23: try 2 fallback variants (1 key each)** before returning to agent
- Variant fallback also fails → classify and return: all-429→rate_limit **retry-after=180s**; has-500/502→api_error; has-timeout→502

## Containers (R24.4)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM | 7 groups × 10 variants = 70 dep (glm5.1 only), ulimits nofile=2048, memory 1GiB |
| auth_to_api_40001 | :40001 | Proxy (all agents) | R24.4 multi-agent gateway: CC/_cc + OpenClaw/_ol + OpenCode/_oc + Hermes/_hm + Codex/_cx |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine (only litellm_glm51 DB) |

## Deploy Method
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

**⚠️ CRITICAL Deploy Order**: ms_uni41001 must be running BEFORE proxy is rebuilt. Proxy sends `glm5.1v1k1` etc to LiteLLM — if LiteLLM doesn't have these model names → "Invalid model name" → CC crash.

**Deploy order**:
1. Copy litellm-glm51/config.yaml → /opt/cc-infra/
2. Copy docker-compose.yml → /opt/cc-infra/
3. Copy gateway/ package → /opt/cc-infra/proxy/gateway/
4. Start ms_uni41001: `cd /opt/cc-infra && docker compose up -d ms_uni41001`
5. Wait for ms_uni41001 healthy: `docker ps` check
6. Rebuild proxy: `cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001`
7. Verify: curl test glm5.1, check /v1/models

## Current Parameters (R24)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Proxy overestimates 1.36x (chars_json/3.0 vs actual) |
| NUM_KEYS | 7 | docker-compose.yml | Keys per model for round-robin |
| NUM_VARIANTS_GLM51 | 10 | docker-compose.yml | R21: variants per key group |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Seconds; P99=85s, max=210s |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (ms_uni41001) | 300 | litellm config.yaml | Seconds |
| num_retries (ms_uni41001) | 0 | litellm config.yaml | Proxy handles all error cycling |
| cooldown_time (ms_uni41001) | 10 | litellm config.yaml | — |
| routing_strategy (ms_uni41001) | simple-shuffle | litellm config.yaml | Proxy specifies exact model |
| All allowed_fails | 0 | litellm config.yaml | LiteLLM pure pass-through |
| API_TIMEOUT_MS | 600000 | settings.json | CC→proxy HTTP total timeout (10min) |

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality

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
- _ol/_oc/_hm cannot connect directly to LiteLLM 41001 (no `glm5.1` alias, only v×k names)
- Direct connection → 400 "Invalid model name"

## 10 Variant Model IDs (ms_uni41001, glm5.1 only)

`ZHIPUAI/GLM-5.1`, `ZHIPUAI/GLm-5.1`, `ZHIPUAI/GlM-5.1`, `ZHIPUAI/Glm-5.1`, `ZHIPUAI/gLM-5.1`, `ZHIPUAI/gLm-5.1`, `ZHIPUAI/glM-5.1`, `ZHIPUAI/glm-5.1`, `ZHIPUAi/GLM-5.1`, `ZHIPUAi/GLm-5.1`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**

## Agent Suffix Model IDs

| Suffix | Agent | Format | Endpoint | Error Cycling |
|--------|-------|--------|----------|---------------|
| `_cc` | Claude Code | Anthropic→OpenAI conversion | /v1/messages | ✅ 429/500/502/timeout |
| `_ol` | OpenClaw | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout |
| `_oc` | OpenCode | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout |
| `_hm` | Hermes | OpenAI passthrough | /v1/chat/completions | ✅ 429/500/502/timeout |
| `_cx` | Codex CLI | Responses↔Chat Completions | /v1/responses | ✅ 429/500/502/timeout |

Frontend model IDs: `glm5.1_cc`, `glm5.1_ol`, `glm5.1_oc`, `glm5.1_hm`, `glm5.1_cx`
Backward compat: `glm5.1` = `glm5.1_cc`, `claude-opus-4-8` = `glm5.1_cc`

## opc2_uname Verification ✅
- gateway module: all files synced
- docker-compose.yml: R24.4 version (3 containers, 40002 removed)
- litellm config: 70 dep glm5.1 only
- 3 containers healthy
- CC settings.json: model=glm5.1_cc, API_TIMEOUT_MS=600000 ✅
- curl test glm5.1_cc via 40001 returns 200 ✅
- Codex CLI end-to-end verified ✅

## opc_uname R24.4 DEPLOYED ✅
- 3 containers healthy (40002 removed)
- Codex CLI end-to-end verified (exec mode: "echo hello world" → output "hello world")
- All 5 agent types (CC/OpenClaw/OpenCode/Hermes/Codex) functional
