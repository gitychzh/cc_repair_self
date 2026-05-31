# Deploy Status — opc_uname (2026-06-01)

## Architecture
```
CC → 40001(proxy, format conversion only) → 41001(LiteLLM glm5.1) → ModelScope
                                          → 42001(LiteLLM dsv4p)  → ModelScope
```

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (77 deployments: 11 variants × 7 keys, rpm=1)
- dsv4p_uni42001 :42001 (77 deployments: 11 variants × 7 keys, rpm=1)
- auth_to_api_40001 :40001 (proxy, 942 lines)
- auth_to_api_40002 :40002 (Codex proxy, framework only)

## Router Settings (updated 2026-06-01)
- num_retries: 3
- cooldown_time: 120 (was 30; KEY5 401 loop every 30s → 120s reduces wasted attempts)
- routing_strategy: latency-based-routing
- lowest_latency_buffer: 0.3 (was 0.1; 2.33:1 key skew → 0.3 improves balance)
- rolling_window_size: 30 (was 10; more stable latency estimates)
- enable_pre_call_checks: false (was true; ROOT CAUSE of CC 401 freeze)
- RateLimitErrorAllowedFails: 3
- TimeoutErrorAllowedFails: 2

## Key Issues Found (2026-06-01)

### MS_KEY5 (`ms-f7231d97-13d8-4049-97d9-378984f4fb2d`) — 401 AuthenticationError
- Key returns 401 on all 22 variants (11 glm5.1 + 11 dsv4p) since 2026-05-31 ~18:50 BJ
- ModelScope known behavior: **quota exhaustion returns 401 instead of 429**
- /v1/models endpoint still returns 200 (key format valid), but /v1/chat/completions = 401
- Account appears "normal" in ModelScope backend — check inference quota, not just account status
- Impact: 213 401 errors in 24h on opc_uname (7.5% of total requests)
- With cooldown_time=120 and enable_pre_call_checks=false, KEY5 401s are properly retried → CC works
- Status: **保留不修改，待用户自行排查额度**

### Root Cause: 401 Freeze Chain (why CC stops working)
```
1. enable_pre_call_checks=true → LiteLLM health check sends max_tokens=5 test
   → ModelScope returns choices=null (GLM-5.1 with max_tokens=5 = empty response)
   → LiteLLM interprets as RateLimitError "Invalid response object"
   → ALL 77 deployments marked unhealthy

2. Request hits KEY5 deployment → 401 AuthenticationError
   → should_retry_this_error: _num_healthy_deployments = 0!
   → "no healthy deployments → don't retry → return 401 directly"

3. Proxy receives 401 → forwards to CC → CC sees AuthenticationError → stops working
```
Fix: enable_pre_call_checks=false → health check disabled → deployments remain available → retry works → CC resilient to KEY5 401

### Deployed Config Sync Issue
- Repo had timeout=180/request_timeout=300 (from opc2_uname round1 fix)
- Deployed /opt/cc-infra had timeout=120/request_timeout=300 (only request_timeout was synced)
- Both machines now synced to repo config

## Key Balance Analysis (glm5.1, 24h data, 6 healthy keys)
| Rank | Total requests | Avg latency | % of total |
|------|---------------|-------------|-----------|
| 1 (busiest) | 535 | 6.9s | 22.8% |
| 2 | 423 | 9.7s | 18.0% |
| 3 | 365 | 9.4s | 15.5% |
| 4 | 328 | 8.2s | 14.0% |
| 5 | 276 | 9.2s | 11.7% |
| 6 (least) | 230 | 8.5s | 9.8% |
| 7 (KEY5) | 191 | 9.5s | 8.1% (401 only) |

Ideal per key: ~16.7%. latency-based-routing causes 2.33:1 skew (highest 22.8% vs lowest 9.8%).
lowest_latency_buffer 0.1→0.3 should improve this over next 24h.

## ModelScope Quota (as of 2026-05-31 15:30 UTC)
| Key | glm5.1 model remaining/200 | glm5.1 requests remaining/2000 | dsv4p model remaining/200 |
|------|---------------------------|------------------------------|--------------------------|
| KEY1 | 111 | 1207 | 189 |
| KEY2 | 147 | 1307 | 188 |
| KEY3 | 124 | 1230 | 189 |
| KEY4 | 139 | 1284 | 187 |
| KEY5 | 0 (401) | 0 (401) | 0 (401) |
| KEY6 | 138 | 1197 | 189 |
| KEY7 | 145 | 1202 | 188 |

## Test Results (2026-06-01)
- glm5.1 OK: Anthropic format, 10/10 success
- dsv4p OK: Anthropic format, success
- Both machines (opc_uname + opc2_uname) synced and tested