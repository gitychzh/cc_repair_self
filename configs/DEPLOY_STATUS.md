# Deploy Status — opc_uname (updated 2026-05-31 22:00 by opc2_uname)

## Architecture
```
CC → 40001(proxy, format conversion + 401 resilience retry) → 41001(LiteLLM glm5.1) → ModelScope
                                                               → 42001(LiteLLM dsv4p)  → ModelScope
```

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (66 deployments: 11 variants × 6 keys, KEY5 revoked)
- dsv4p_uni42001 :42001 (66 deployments: 11 variants × 6 keys, KEY5 revoked)
- auth_to_api_40001 :40001 (proxy, format conversion + MODEL_MAP + 401 resilience retry)
- auth_to_api_40002 :40002 (Codex proxy, same codebase)

## opc2_uname_r2 Changes (2026-05-31)

### KEY5 Removed from glm51 Config (CRITICAL FIX)
- **Before**: glm51 config had 77 deployments (11×7 keys including KEY5)
- **After**: 66 deployments (11×6 keys, KEY5 removed like dsv4p)
- **Why**: KEY5 (ms-bb4e5bee = duplicate of KEY1, actual revoked key ms-f7231d97 returns 401) was still in glm51 config despite being documented as revoked. 11 redundant deployments consuming KEY1 quota twice with no capacity gain.
- **docker-compose**: MS_KEY5 env var removed from both services, replaced with revocation comment

### Router Settings Optimized (both configs)
- `cooldown_time`: 120→60 — 2min cooldown removed too much capacity from small 66 RPM pool. 60s balances recovery vs capacity loss.
- `lowest_latency_buffer`: 0.3→0.1 — 30% buffer was too wide at rpm=1 (negligible load differences). 10% allows slight load balancing without wasting latency.
- `RateLimitErrorAllowedFails`: 3→1 — At rpm=1, rate-limit error means definitive quota exhaustion. 3 allowed fails wasted requests on already-limited deployments.
- `AuthenticationErrorAllowedFails`: 0 added to glm51 (was missing! caused 401 freeze risk)
- Duplicate `AuthenticationErrorAllowedFails: 0` line removed from dsv4p

### glm51 Config Parity with dsv4p
- `store_prompts_in_spend_logs: true` added (was missing in glm51)
- `timeout`: 180→300 per deployment (matches dsv4p, allows retries within request_timeout=300)

### Proxy Streaming Bug Fixes (CRITICAL)
1. **Stream connection errors unhandled** → Wrapped streaming loop in try/except, catches RemoteDisconnected/timeout/ConnectionReset/OSError. Emits graceful close (content_block_stop + message_delta + message_stop) instead of crashing.
2. **Missing message_delta when stream ends without [DONE]** → New `_emit_graceful_end()` helper always emits message_delta + message_stop.
3. **Missing message_start when stream ends with no content** → `_emit_graceful_end()` emits synthetic message_start if needed.
4. **Nested function scope bug** → `message_start_sent`/`message_delta_sent`/`active_block_type` needed `nonlocal` declarations to actually modify outer scope. Without nonlocal, helper functions created new local variables instead — state was never actually tracked.
5. **Thinking signature missing in streaming** → `content_block_start` for thinking blocks now includes `signature` field.
6. **Byte-by-byte streaming** → Changed from `resp.read(1)` to `resp.read(8192)` for ~8000x better throughput.
7. **Malformed JSON chunks silently skipped** → Now logged as WARN instead of silently dropped.
8. **Duplicate _log_lock definition** → Removed second definition (lines 102/106 were both `threading.Lock()`).

### Docker Healthcheck Improvement
- Proxy containers (40001/40002) healthcheck changed from GET /health to HEAD /health — lighter weight, no response body parsed.

## Router Settings (updated 2026-05-31)
- num_retries: 3
- cooldown_time: 60
- routing_strategy: latency-based-routing
- lowest_latency_buffer: 0.1
- rolling_window_size: 30
- enable_pre_call_checks: false (ROOT CAUSE of CC 401 freeze — see below)
- background_health_checks: false (NEW: /health endpoint triggers on-demand health check → same choices=null problem)
- AuthenticationErrorAllowedFails: 0 (immediate cooldown on 401)
- RateLimitErrorAllowedFails: 1 (definitive quota exhaustion at rpm=1)
- TimeoutErrorAllowedFails: 2

## Key Issues Found (2026-06-01)

### MS_KEY5 (`ms-f7231d97-13d8-4049-97d9-378984f4fb2d`) — 401 AuthenticationError
- Key returns 401 on all 22 variants since 2026-05-31 ~18:50 BJ
- ModelScope: **quota exhaustion returns 401 instead of 429**
- Status: **保留不修改，待用户自行排查额度**

### Root Cause: 401 Freeze Chain (updated 2026-06-01)
```
1. enable_pre_call_checks=true OR /health endpoint call → health check sends max_tokens=5
   → ModelScope returns choices=null (GLM-5.1 with max_tokens=5 = empty response)
   → LiteLLM interprets as RateLimitError "Invalid response object"
   → ALL 77 deployments marked unhealthy

2. Request hits KEY5 deployment → 401 AuthenticationError
   → should_retry_this_error: _num_healthy_deployments = 0!
   → "no healthy deployments → don't retry → return 401 directly"

3. Proxy receives 401 → forwards to CC → CC sees AuthenticationError → stops working
```
Fixes applied (三层防御):
1. **enable_pre_call_checks=false** — 阻止请求前health check
2. **background_health_checks=false** — 阻止后台health check (glm51 config之前缺少此项)
3. **proxy 401 resilience retry** — proxy收到401 AuthenticationError时自动重试一次，让LiteLLM选择不同deployment（KEY5已cooldown）

### /health Endpoint Trigger Problem
- Calling `/health` endpoint (even once) triggers on-demand health check → same choices=null → deployments marked unhealthy
- `/health/liveliness` is safe (Docker health check uses this, no side effects)
- **Never call `/health` for monitoring — use `/health/liveliness` only**
- background_health_checks=false prevents periodic health checks, but `/health` endpoint call still triggers on-demand check

### Proxy URL Path Bug (fixed)
- opc2_uname docker-compose env `LITELLM_URL_GLM51=http://host:4000` (no path) → proxy forwarded to bare host → 405 Method Not Allowed
- Fixed: `_ensure_url_path()` helper auto-appends `/v1/chat/completions` if env var lacks path

### MODEL_MAP Not Applied to Forwarded Requests (fixed 2026-05-31)
- **Root cause of `400 Invalid model name model=claude-opus-4-7` error**
- MODEL_MAP defined mappings (claude-opus-4-7→glm5.1 etc.) but was never applied to the model name sent to LiteLLM
- Two forwarding paths both bypassed MODEL_MAP:
  1. `/v1/messages`: `anth_to_openai(anth_body)` took raw model name from request body → LiteLLM received `claude-opus-4-7` instead of `glm5.1`
  2. `/chat/completions`: MODEL_MAP used for upstream routing but raw body forwarded unchanged → same model name issue
- Proxy logs confirmed: `model=claude-opus-4-8→claude-opus-4-8` (no mapping applied)
- After fix: `model=claude-opus-4-8→glm5.1` (mapping correctly applied)
- 4×400 errors observed in error_detail logs at 19:37-19:42 (before fix deployment)

### _stream_to_anth delta UnboundLocalError (fixed 2026-05-31)
- Line 820 referenced `delta.get()` before line 826 defined `delta = chunk_data.get(...)`
- Caused streaming requests to crash with 502 "cannot access local variable 'delta' where it is not associated with a value"
- 2×502 crashes observed in logs at 19:29:00 and 19:29:10
- Fix: moved delta/finish_reason definitions before first usage

### Streaming Tool Call Parse Error (fixed 2026-06-01)
- **Root cause of "The model's tool call could not be parsed (retry also failed)"**
- Bug 1: When OpenAI first tool call chunk has both `id` AND partial `arguments` (e.g., `{"`), the `if tc.get("id")` branch only emits `content_block_start` — the `elif` branch for arguments is never reached. The opening brace of JSON args is silently dropped → concatenated `partial_json` is invalid JSON.
- Bug 2: `message_start` missing `stop_sequence: None` and cache token fields in usage.
- Bug 3: Empty stream edge case — `[DONE]` without `message_start` → CC receives only `message_stop`.
- Bug 4: Stream interrupted without `[DONE]` — no `content_block_stop` or `message_stop` emitted → CC gets incomplete message.
- Fix: emit `input_json_delta` after `content_block_start` when first chunk includes arguments; add missing Anthropic fields; add graceful fallback for empty/broken streams; remove dead `_stream_chunk_to_anth` function.

## Test Results (2026-05-31, opc2_uname proxy rebuilt)
- claude-opus-4-7 → glm5.1: ✅ 200
- claude-opus-4-8 → glm5.1: ✅ 200
- claude-sonnet-4-6 → glm5.1: ✅ 200 (intermittent 500 from ModelScope choices=None, non-proxy bug)
- dsv4p → dsv4p: ✅ 200
- delta crash: ✅ no more UnboundLocalError after rebuild
- 400 Invalid model: ✅ no more after MODEL_MAP fix deployment