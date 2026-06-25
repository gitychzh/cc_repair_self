# Deploy Status — opc_uname + opc2_uname (R44, 2026-06-25)

## Architecture (R44: LiteLLM→ms-gateway + null-safety fix)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (auto-fallback relay, Connection:close relay, PROXY_TIMEOUT deadline)
      ├── PRIMARY  → :40005 proxy (EXPERIMENT, MS-first + NV 2-tier last-resort fallback)
      └── FALLBACK → :40001 proxy (STABLE, pure MS, Connection:close on all responses)

:40005  cc-proxy → _cc /v1/messages → MS-first (ALL requests go to MS first)
  MS success → done (fast, ~2.3s avg)
  MS all-429 → NV 3-tier last-resort fallback (R42: deepseek→kimi→glm5.1)
    Tier 1: deepseek (deepseek-ai/deepseek-v4-pro) → all 5 NV keys RR → all-fail →
    Tier 2: kimi (moonshotai/kimi-k2.6) → all 5 NV keys RR → all-fail →
    Tier 3: glm5.1 (z-ai/glm-5.1) → all-fail → ABORT
    per-tier persistent RR counter (not restarting from k1)
    NV_TIER_TIMEOUT_BUDGET_S=45s caps total NV fallback time (R41-1: 90→45)
    R38.8: NV conn-fast-break (2 consecutive connection errors → skip to next tier)
    Budget checked before each tier start and before each key attempt
  NV_TIMEOUT=30s (p50=13.4s, p80=~30s → captures 80% viable NV requests)
  Connection:close on all proxy responses (prevents keep-alive BrokenPipe cascade)
:40001  cc-proxy → _cc /v1/messages → pure MS glm5.1 v×k cycling (NV disabled, stable baseline)
:40002  codex-proxy → _cx /v1/responses → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  passthrough-proxy → _ol/_oc/_hm_ms → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."
  _hm_ms suffix for Hermes MS fallback endpoint (R38.4: _hm_ms = Hermes + ModelScope)

── 外部 app endpoint（不属于 cc-infra 核心）──
:40006  hm-proxy → _hm_nv /v1/chat/completions → ALL models via NVCF pexec (SOCKS5 → ACTIVE functions)
  R42: Tier reorder — deepseek primary (1-3s, 100% rate) → kimi → glm5.1
  NVCF pexec direct path (bypasses integrate API entirely)
    deepseek → orion-deepseek-v4-pro (ACTIVE), all params pass through ✅
    glm5.1 → ai-glm5_1 (ACTIVE), strips thinking_budget (NVCF rejects it ❌) ✅
    kimi → nvquery-kimi-k2.6 (ACTIVE), all params pass through ✅
  No LiteLLM routing — hm40006 connects directly via SOCKS5 proxy per-key mihomo
  R38.13: LiteLLM 41101-41105 containers REMOVED
  默认 deepseek_hm_nv(NVCF pexec, primary) → kimi_hm_nv → glm5.1_hm_nv → 全失败 → ABORT
  TIER_TIMEOUT_BUDGET_S=45s
  fallback 从当前位置继续（不是从k1），per-tier persistent RR counter
  NV_MODEL_IDS: deepseek_hm_nv/kimi_hm_nv/glm5.1_hm_nv (3-tier chain active)
  Hermes: ~/.hermes-venv/bin/hermes → config in ~/.hermes/config.yaml

→ :41001 ms-gateway (R44: replaced LiteLLM, python:3.11-alpine, ~65.7MB, ~2s startup)
  Pure passthrough — resolves model_name→MS variant ID+key, direct HTTPS to ModelScope
  HTTP/1.1 + Transfer-Encoding:chunked for SSE streaming (R44 fix)
  No routing, no retries, no cooldown, no DB — cc-proxy handles all intelligence
→ :7894-7899 mihomo ♻️US-NV-K1~K5 → NVIDIA API (health-check url = NV API, interval=180s)
```

## Containers (R44: 7 core + 1 external + 1 ms-gateway + 1 DB = 10 total)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Content-Length fix + PROXY_TIMEOUT deadline |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, _hm_ms suffix for Hermes MS fallback |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-first + NV last-resort, NV_TIMEOUT=30 |
| hm40006 | :40006 | hm-proxy(external) | 1CPU/1GiB | R42: deepseek primary + NVCF pexec all 3 models |
| ms_uni41001 | :41001 | ms-gateway | 1CPU/1GiB | R44: replaced LiteLLM, python:3.11-alpine, 70 models |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 (used by hm-proxy only) |

## R44 Changes (opc_uname, 2026-06-25) — LiteLLM→ms-gateway + null-safety fix

### Root Cause: Two Independent Bugs Breaking SSE Streaming

1. **HTTP/1.0 protocol mismatch**: ms-gateway (BaseHTTPRequestHandler) sent HTTP/1.0 responses,
   but cc-proxy's HTTPConnection (HTTP/1.1 client) couldn't read SSE data incrementally.
   HTTP/1.0 "read until close" semantics block until the entire stream finishes or connection closes,
   then either returns all data at once or IncompleteRead(0 bytes).

2. **null-safety gap in stream.py**: LiteLLM filtered null fields from MS SSE responses;
   without LiteLLM, raw MS SSE has `tool_calls: null`, `choices: []`, etc.
   Python `dict.get(key, default)` returns None when key exists but value is null
   (only returns default when key doesn't exist at all).
   `for tc in None:` → TypeError: 'NoneType' object is not iterable.

### Fix 1: ms-gateway HTTP/1.1 + chunked streaming
- handler.py: `protocol_version = "HTTP/1.1"` (was HTTP/1.0 default)
- handler.py: streaming response uses `Transfer-Encoding: chunked` header
- upstream.py: `stream_passthrough_chunked()` — wraps each SSE chunk as HTTP chunked format:
  `hex_size\r\n data\r\n`, terminated with `0\r\n\r\n`
- Each chunk flushed to wfile immediately → cc-proxy can read incrementally

### Fix 2: cc-proxy stream.py null-safety
- `delta.get("choices", [{}])` → `chunk_data.get("choices") or [{}]` (applies to both stream paths)
- `delta.get("tool_calls", [])` → `delta.get("tool_calls") or []`
- `chunk_data.get("usage", {})` → `chunk_data.get("usage") or {}`
- `delta.get("content", "")` → `delta.get("content") or ""`
- `delta.get("reasoning_content", "")` → `delta.get("reasoning_content") or ""`

### Verified Working (opc_uname remote)
- 40000→40005→ms-gateway→MS: streaming ✓
- 40000→40005→ms-gateway→MS: non-streaming ✓
- 40001→ms-gateway→MS: streaming ✓
- ms-gateway direct: streaming ✓, non-streaming ✓
- ms-gateway /health ✓, /v1/models (73 models) ✓

### Why LiteLLM Was Zero-Value in CC Chain (data-driven)
- num_retries=0: LiteLLM never retries
- AllowedFails=0: LiteLLM never marks dep as failed
- simple-shuffle routing BUT cc-proxy specifies exact model_name (glm5.1v3k5)
  → LiteLLM has exactly 1 dep per model_name → no routing choice
- LiteLLM adds ~500ms latency per request (Flask request parsing + OpenAI SDK overhead)
- LiteLLM image: ~1.5GB, startup ~60s; ms-gateway: ~65.7MB, startup ~2s

### Resource Savings
- Image size: ~1.5GB → ~65.7MB (~23x reduction)
- Startup time: ~60s → ~2s (~30x improvement)
- RAM usage: ~500MB → ~50MB (~10x reduction)
- CPU: near-zero (pure HTTP passthrough, no Flask/OpenAI SDK overhead)

## Deploy Method
```bash
# Step 1: sync configs
bash ~/cc_ps/cc_repair_self/scripts/sync_config.sh

# Step 2: rebuild (code changes must rebuild!)
cd /opt/cc-infra && docker compose up -d --build --force-recreate ms_uni41001 auth_to_api_40005 auth_to_api_40001 auth_to_api_40000

# Step 3: verify
curl -sf http://127.0.0.1:40000/health && curl -sf http://127.0.0.1:40005/health
curl -sf http://127.0.0.1:41001/health  # ms-gateway
curl -s -X POST http://127.0.0.1:40005/v1/messages \
  -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50,"stream":true}'
```

## History (condensed)
- R30-31: counter persistence, dual CC proxy, dispatcher, 429 truth, throttle
- R32: glm5.2→5.1 revert
- R33-34: NV LiteLLM (failed), direct NV API tunnel
- R35: dispatcher auto-fallback, blue-green self-optimization, NV disabled (R35.1-15)
- R35.5: dsv4p permanent removal (140→70 dep)
- R35.6: OpenClaw stuck fix + Ghost metrics
- R35.7-8: 5 bug fixes, stale deploy, throttle alignment 2→1.5
- R35.9: SSE buffer parsing (FR 1.9%→85.7%)
- R35.10: dispatcher path fix + MSG-FIX
- R35.11-15: Verification → system stable (99.1%, 0% ABORT)
- R36: NV re-enablement (5-key alternating, per-key proxy, NV_TIMEOUT=60)
- R36.1: NV LiteLLM containers (41101-41105)
- R36.2: Container standardization (1CPU/1-2GiB, Docker proxy, mihomo, NV read timeout)
- R36.3: Dead code cleanup (410行), dispatcher fixes, ms_uni41001 2GiB, throttle lock-free
- R36.5: MS-first + NV last-resort (NV alternating 纯负优化 → 56% throughput reduction)
- R37: Hermes专用 NV proxy hm40006 + 5 NV HM LiteLLM (41101-41105, DATABASE_URL bug, not working)
- R38: Hermes 重新工程化 — hm40006 路由到 LiteLLM 41101-41105 + per-key mihomo + STORE_MODEL_IN_DB=False + 清理 _hm suffix
- R38.1-13: 见 CLAUDE.md + 旧 DEPLOY_STATUS
- R38.14: HM tier reorder glm5.1 primary + budget enforcement per-attempt + misleading timeout log
- R40: hm-proxy ring fallback + budget fix + DB persistence + error info enhancement
- R41: hm-proxy DB poison-batch fix (残缺 _log_metrics dict → batch INSERT atomic fail → 整批 50 行丢失)
- R42: NV-TIER-SKIP fix + deepseek primary reorder
- R44: LiteLLM→ms-gateway (python:3.11-alpine) + HTTP/1.1 chunked streaming + stream.py null-safety
