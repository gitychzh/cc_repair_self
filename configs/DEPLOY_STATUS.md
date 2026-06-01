# Deploy Status — opc_uname (updated 2026-06-02 by opc2_uname)

## Architecture
```
CC → 40001(proxy, format conversion + force-stream ALL non-stream) → 41001(LiteLLM glm5.1) → ModelScope
                                                                     → 42001(LiteLLM dsv4p)  → ModelScope
```

## Deploy Method
- **docker compose**: `cd /opt/cc-infra && DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001`
- **Docker Hub**: unreachable from China without proxy → mihomo on port 7880 configured as Docker systemd proxy (`/etc/systemd/system/docker.service.d/proxy.conf`)
- **Legacy builder**: `DOCKER_BUILDKIT=0` required — BuildKit doesn't respect systemd proxy

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (77 deployments: 11 variants × 7 keys)
- dsv4p_uni42001 :42001 (77 deployments: 11 variants × 7 keys)
- auth_to_api_40001 :40001 (proxy, format conversion + MODEL_MAP + DSv4P force-stream + proper error mapping)
- auth_to_api_40002 :40002 (Codex proxy, same codebase)

## opc2_uname_r3 Changes (2026-06-02)

### CRITICAL: All 11 Variants Restored (both configs)
- **Before**: glm5.1 had 28 deployments (4 variants × 7 keys), dsv4p had 14 (2 variants × 7 keys)
- **After**: 77 deployments each (11 variants × 7 keys)
- **Why**: Previous config removed 7 glm5.1 variants (v5-v11) and 9 dsv4p variants (v3-v11) claiming they returned "choices=null from ModelScope". **This was wrong** — direct API testing confirmed ALL 22 variants return HTTP 200 with valid choices. The root cause was ModelScope non-stream responses including a `delta` field (invalid for OpenAI non-stream format), which crashed LiteLLM's parser. The force-stream fix (deployed 2026-06-01) resolves this for ALL variants. Removing variants = removing quota capacity (200/id/day per variant). 7 glm5.1 variants removed = 1400/id/day quota lost. 9 dsv4p variants removed = 1800/id/day quota lost.
- **Evidence**: Tested all 22 variants directly against ModelScope API on 2026-06-02 — all returned HTTP 200 with valid `choices[0].message.content`.
- **Lesson reinforced**: [[verify-before-delete]] — NEVER remove resources without verifying their independent value first. The "null-response" diagnosis was a LiteLLM parser bug, not a ModelScope API bug.

### Router Settings Reverted to Optimal Values (both configs)
- `num_retries`: 5→3 — With 77 deployments, 5 retries wastes latency. LiteLLM's latency-based-routing finds a working deployment faster with 3 retries on a larger pool.
- `RateLimitErrorAllowedFails`: 3→1 — At rpm=1, rate-limit means definitive quota exhaustion. With 77 deployments, allowing 3 fails wastes requests on already-limited deployments.
- `TimeoutErrorAllowedFails`: 3→2 — Same reasoning. More deployments = less tolerance needed.
- `rolling_window_size`: 300→30 — 300-window is too slow for routing adaptation at rpm=1. Shorter window allows faster shift to less-loaded deployments.
- `BadRequestErrorAllowedFails`: 0 removed — BadRequest is a client error, not a deployment health indicator. No deployment should be cooled down for a BadRequest.

### Previous Changes Still Active (from opc2_uname_r2, 2026-05-31)
- KEY5 removed from both configs (ms-f7231d97 returns 401 = quota exhaustion)
- `cooldown_time`: 60 (from 120)
- `lowest_latency_buffer`: 0.1 (from 0.3)
- `enable_pre_call_checks`: false (prevents 401 freeze chain)
- `background_health_checks`: false (prevents health check cascade)
- `AuthenticationErrorAllowedFails`: 0 (immediate cooldown on 401)
- Proxy force-stream for ALL non-stream requests
- Proxy streaming bug fixes (graceful end, byte-by-byte, etc.)
- Proxy error mapping (429→rate_limit_error, 400 InvalidParameter→api_error)

## Router Settings (updated 2026-06-02)
- num_retries: 3
- cooldown_time: 60
- routing_strategy: latency-based-routing
- lowest_latency_buffer: 0.1
- rolling_window_size: 30
- enable_pre_call_checks: false
- background_health_checks: false
- AuthenticationErrorAllowedFails: 0 (immediate cooldown on 401)
- RateLimitErrorAllowedFails: 1 (definitive quota exhaustion at rpm=1)
- TimeoutErrorAllowedFails: 2

## Metrics Summary (2026-06-01, before variant restoration)
- Total requests: 428
- Success rate: 86.0% (368/428)
- Avg latency: 18684ms (glm5.1)
- P50 latency: 14352ms (glm5.1)
- P95 latency: 38807ms (glm5.1)
- Error breakdown: 502=20, 429=19, 529=15, 400=4, ConnectionRefused=20, InputTooLong=18
- Note: With only 28+14=42 deployments, rate-limit errors are frequent. After restoring to 77+77=154, rate-limit errors should decrease significantly (3.6x more quota capacity).

## Key Issues Found

### ModelScope Non-Stream InternalServerError — FIXED (2026-06-01)
- **Root cause**: ModelScope non-stream responses include `delta` field → invalid for OpenAI non-stream format → LiteLLM assertion fails → choices=None → InternalServerError
- **Fix**: ALL non-stream requests force `stream=True` to LiteLLM. Proxy collects streaming chunks and synthesizes non-stream Anthropic response. This works for ALL 22 variants (confirmed by direct API testing on 2026-06-02).

### MS_KEY5 (`ms-f7231d97`) — 401 AuthenticationError
- Key returns 401 on all variants since 2026-05-31
- ModelScope: quota exhaustion returns 401 instead of 429
- Status: KEY5 deployments still in config (7 per model), but cooldown after first 401

### Root Cause: 401 Freeze Chain (fixed 2026-05-31)
```
1. enable_pre_call_checks=true → health check sends max_tokens=5
   → ModelScope returns choices=null → LiteLLM marks ALL deployments unhealthy
2. Request hits KEY5 deployment → 401 → no healthy deployments → no retry → return 401
3. Proxy receives 401 → forwards to CC → CC sees AuthenticationError → stops working
```
Fixes: enable_pre_call_checks=false, background_health_checks=false, proxy 401 resilience retry

### /health Endpoint (never call /health for monitoring — use /health/liveliness only)

### Proxy Streaming Bug Fixes (2026-06-01)
- Stream connection errors handled → graceful close instead of crash
- Missing message_delta when stream ends without [DONE]
- Byte-by-byte → 8192 byte chunks for better throughput
- Thinking signature in streaming blocks
- Tool call first chunk arguments not dropped

### thinking_budget InvalidParameter Fix (2026-06-01)
- Preflight check adjusts max_completion_tokens = budget_tokens + 8192
- Prevents 400 error at format conversion stage

## Test Results (2026-06-02, after variant restoration)
- glm5.1 non-stream: ✅ 200 (force-stream + collect works)
- glm5.1 stream: ✅ 200
- dsv4p non-stream: ✅ 200 (force-stream + collect works)
- dsv4p stream: ✅ 200
- claude-opus-4-7→glm5.1: ✅ 200 (MODEL_MAP working)
- glm5.1 deployments: 77 ✅ (11 variants × 7 keys)
- dsv4p deployments: 77 ✅ (11 variants × 7 keys)