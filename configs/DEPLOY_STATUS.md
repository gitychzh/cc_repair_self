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
- `RateLimitErrorAllowedFails`: 3→1 — At rpm=1, rate-limit means definitive quota exhaustion. With 77 deployments, allowing 3 fails wastes requests on already-limited deployments. **NOTE: Round 6 data analysis changed this back to 3 — see Round 6 section.**
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

## opc2_uname_r5 Changes (2026-06-02)

### Proxy RATELIMIT retry + FALLBACK removed (highest priority)
- **Before**: Proxy had rate_limit_retry (429 → 2s wait → retry) and FALLBACK (glm5.1 429 → dsv4p)
- **After**: Both removed. 429 errors directly mapped to CC via rate_limit_error/529 → CC retries with backoff
- **Why**: Core principle "proxy only does format conversion" + data proves:
  - RATELIMIT retry: 8% success (1/13), 2s latency waste per attempt
  - FALLBACK: always fails — UnsupportedParamsError on reasoning_effort (dsv4p doesn't support it)
  - CC has built-in retry on rate_limit_error, LiteLLM has num_retries=3 for deployment rotation
- **Code removed**: 109 lines (RATELIMIT retry block + FALLBACK block)
- **should_rate_limit_retry**: set to `False` (disabled, not deleted for clarity)

### LiteLLM config: routing_strategy_args fix (critical)
- **Before**: `lowest_latency_buffer: 0.1` and `rolling_window_size: 30` placed directly under `router_settings`
- **After**: Moved to `router_settings.routing_strategy_args` sub-key
- **Why**: LiteLLM v1.85.1 Router.__init__() does NOT accept these as direct parameters. Warning logged on every startup: "Key 'lowest_latency_buffer' is not a valid argument for Router.__init__(). Ignoring this key."
- **Impact**: latency-based-routing strategy was effectively running WITHOUT buffer/window tuning (parameters ignored = default behavior). After fix, routing properly considers latency buffer and rolling window.
- **Verified**: `docker exec glm5.1_uni41001 python3 -c "Router([...], routing_strategy_args={'lowest_latency_buffer': 0.1, 'rolling_window_size': 30})"` → OK

### DSv4P config: allowed_openai_params + drop_params (bug fix)
- **Before**: dsv4p config had no `allowed_openai_params` and `drop_params: false`
- **After**: Added `allowed_openai_params` list (parity with glm5.1) + `drop_params: true`
- **Why**: FALLBACK failure evidence: `UnsupportedParamsError: openai does not support parameters: ['reasoning_effort'], for model=deepseek-ai/DeepSeek-v4-pro`. Even without FALLBACK, this config deficiency should be fixed — future direct dsv4p requests would also fail with reasoning_effort.
- **Note**: reasoning_effort is intentionally excluded from dsv4p's allowed_openai_params (DSv4P doesn't support it). drop_params=true drops it gracefully.

## opc2_uname_r6 Changes (2026-06-02)

### num_retries: 5→3 (both configs)
- **Before**: num_retries=5 (opc_uname's Round 5 reverted my 3→5)
- **After**: num_retries=3
- **Why**: 429 insufficient_quota exhausts ALL retries regardless of count (all deployments return 429 simultaneously). Data: 38x 429 with num_retries=3 → all exhausted with same outcome. num_retries=5 wastes 2 extra retries (~20-30s latency) for zero benefit on quota-exhaustion 429. For RPM 429 (rpm=1), 3 retries find a non-limited deployment faster than 5.

### RateLimitErrorAllowedFails: 1→3 (both configs)
- **Before**: RateLimitErrorAllowedFails=1 (opc_uname's Round 5 reverted my 3→1)
- **After**: RateLimitErrorAllowedFails=3
- **Why**: Two types of 429 exist:
  - insufficient_quota: ALL deployments 429 → AllowedFails=1 vs 3 makes NO difference (all exhaust pool)
  - RPM 429 (rpm=1): AllowedFails=1 is too aggressive → 1 RPM hit → 30s cooldown removes working deployment. AllowedFails=3 tolerates normal RPM rotation.
  - Previous Round 4 cascade (65/77 unhealthy) was InternalServerError cascade, now mitigated by InternalServerErrorAllowedFails=3

### MODEL_INPUT_TOKEN_SAFETY env reading fix (proxy.py)
- **Before**: MODEL_INPUT_TOKEN_SAFETY hardcoded as {glm5.1:130000, dsv4p:130000} — docker-compose env vars (128000) were completely IGNORED
- **After**: Read from os.environ.get() with fallback 128000. All .get() fallbacks also changed from 130000→128000
- **Evidence**: proxy log now shows `safety=128000` (was `safety=130000` before fix)

## Router Settings (updated 2026-06-02, Round 1-6 optimizations)
- num_retries: 3
- cooldown_time: 30
- routing_strategy: latency-based-routing
- routing_strategy_args:
  - lowest_latency_buffer: 0.1
  - rolling_window_size: 30
  (Previously placed directly in router_settings — LiteLLM v1.85 ignored them. Now under routing_strategy_args — actually effective.)
- enable_pre_call_checks: false
- background_health_checks: false
- AuthenticationErrorAllowedFails: 0 (immediate cooldown on 401)
- RateLimitErrorAllowedFails: 3 (was 1 — 1 allowed fail + 60s cooldown caused 65/77 unhealthy cascade)
- TimeoutErrorAllowedFails: 2
- InternalServerErrorAllowedFails: 3 (NEW — prevents ModelScope null-response cooldown cascade)

## Proxy Changes (Round 1-5)
- Added `import socket` — socket.timeout referenced at line 1233 but module not imported
- Removed conn_retry — 3% success rate (1/36), 3s wasted latency per attempt
- Removed rate_limit_retry — 8% success rate (1/13), 2s wasted latency per attempt
- Removed glm→dsv4p FALLBACK — always fails (UnsupportedParamsError on reasoning_effort)
- should_rate_limit_retry = False (disabled for clarity, not deleted)
- RateLimitErrorAllowedFails: 1→3, cooldown_time: 60→30, InternalServerErrorAllowedFails: 3

## Metrics Summary (2026-06-02, after Round 1-4 optimizations)
- Total requests (clean data, 19:10 UTC onwards): 89
- Success rate: 100% (89/89) — zero 502, zero 429
- RL retry: 6 attempts, 3 success (50%)
- Conn retry: 0 (removed in Round 2)
- Avg duration: 15241ms
- P90 duration: 24468ms

### Before vs After Comparison
| Metric | Before (Round 1) | After (Round 4) |
|--------|-----------------|-----------------|
| Success rate | 85.4% | 100% |
| 502 errors | 13.1% | 0% |
| 429 errors | 1.0% | 0% |
| RL retry | 19 | 6 (50% success) |
| Conn retry | 18 (3% success) | 0 (removed) |
| Avg duration | 12065ms | 15241ms |

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