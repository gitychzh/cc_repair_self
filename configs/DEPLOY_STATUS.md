# Deploy Status — opc_uname (2026-06-01)

## Architecture
```
CC → 40001(proxy, format conversion + 401 resilience retry) → 41001(LiteLLM glm5.1) → ModelScope
                                                               → 42001(LiteLLM dsv4p)  → ModelScope
```

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (77 deployments: 11 variants × 7 keys, rpm=1)
- dsv4p_uni42001 :42001 (77 deployments: 11 variants × 7 keys, rpm=1)
- auth_to_api_40001 :40001 (proxy, ~1000 lines, format conversion + 401 resilience retry)
- auth_to_api_40002 :40002 (Codex proxy, framework only)

## Router Settings (updated 2026-06-01)
- num_retries: 3
- cooldown_time: 120
- routing_strategy: latency-based-routing
- lowest_latency_buffer: 0.3
- rolling_window_size: 30
- enable_pre_call_checks: false (ROOT CAUSE of CC 401 freeze — see below)
- background_health_checks: false (NEW: /health endpoint triggers on-demand health check → same choices=null problem)
- RateLimitErrorAllowedFails: 3
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

## Test Results (2026-06-01)
- opc_uname: glm5.1 OK, dsv4p OK
- opc2_uname: glm5.1 5/5 OK, dsv4p OK
- Both machines fully synced with new configs