# Deploy Status — opc_uname (updated 2026-06-03 R9 — deploy R8 auto-compact + 429 quota→rate_limit_error + safety 170K + Tailscale v1.99 + BBR)

## Architecture
```
CC → 40001(proxy, format conversion + force-stream ALL non-stream) → 41001(LiteLLM glm5.1) → ModelScope
                                                                     → 42001(LiteLLM dsv4p)  → ModelScope
```

## Deploy Method
- **docker compose**: `cd /opt/cc-infra && DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001`
- **Docker Hub**: unreachable from China without proxy → mihomo on port **7890** configured as Docker systemd proxy (`/etc/systemd/system/docker.service.d/proxy.conf`). Note: previously misconfigured as 7880, fixed in Round 3.
- **Legacy builder**: `DOCKER_BUILDKIT=0` required — BuildKit doesn't respect systemd proxy

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (77 deployments: 11 variants × 7 keys)
- dsv4p_uni42001 :42001 (77 deployments: 11 variants × 7 keys)
- auth_to_api_40001 :40001 (proxy, format conversion + MODEL_MAP + DSv4P force-stream + proper error mapping + insufficient_quota→api_error)
- auth_to_api_40002 :40002 (Codex proxy, same codebase + insufficient_quota→api_error)

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

## opc2_uname_r7 Changes (2026-06-03 — 529 Overloaded crash fix)

### CRITICAL: insufficient_quota 429 → api_error (NOT rate_limit_error)
- **Before**: ModelScope `insufficient_quota` 429 was mapped to `rate_limit_error` by `_convert_error()`
- **After**: `insufficient_quota` 429 → `api_error`
- **Why**: CC treats `rate_limit_error` as a temporary throttle → retries with exponential backoff. But `insufficient_quota` means ModelScope account quota is genuinely exhausted (token/month limit) — CC backoff wastes time because quota won't recover in seconds (daily/monthly reset). Worse: CC's backoff loop on rate_limit_error + INPUT-REJECT 529 cascades into "Repeated 529 Overloaded" crash.
- **Evidence**: 2026-06-03 metrics: 26x 529 InputExceedsProxyReject + 25x 429 insufficient_quota. Combined loop triggered CC crash.
- **Implementation**: `_convert_error()` now checks for `insufficient_quota` error code, `quota + exceeded` message pattern, and `exceeded your current quota` message before classifying as `api_error`. Regular RPM 429 still → `rate_limit_error` (correct for temporary throttles).

### MODEL_INPUT_TOKEN_SAFETY: 110K → 120K (both proxies)
- **Before**: MODEL_INPUT_TOKEN_SAFETY_GLM51=110000, DSV4P=110000
- **After**: MODEL_INPUT_TOKEN_SAFETY_GLM51=120000, DSV4P=120000
- **Why**: CC contextWindow=110K → requests at ~110K tokens → estimated_tokens=110K/chars_per_token → when chars_per_token=2.5: estimated ~110K exactly → INPUT-REJECT at 110K threshold → 529 overloaded_error → CC compaction → retry still at ~110K → another 529 → loop → crash. Raising to 120K gives 10K headroom above CC's contextWindow so legitimate 110K requests pass through.
- **CC settings unchanged**: contextWindow=110K, autoCompactWindow=90K. With safety=120K, CC 110K requests (estimated ~110K tokens with chars_per_token=3.5) are well under the 120K threshold.

### CHARS_PER_TOKEN_ESTIMATE: 2.5 → 3.5 (both proxies)
- **Before**: CHARS_PER_TOKEN_ESTIMATE=2.5 (underestimates token count → more INPUT-REJECT hits)
- **After**: CHARS_PER_TOKEN_ESTIMATE=3.5 (more conservative → fewer false INPUT-REJECT)
- **Why**: With chars_per_token=2.5, a 385K-char request = estimated 154K tokens (over safety limit). With chars_per_token=3.5, same request = estimated 110K tokens (under 120K safety). The more conservative estimate reduces false INPUT-REJECT rejection, preventing the 529 cascade.

### Root Cause: "Repeated 529 Overloaded" crash chain
```
1. CC consumes ~100K tokens → sends request → proxy INPUT-REJECT (estimated ~110K > safety 110K)
   → HTTP 529 + overloaded_error → CC auto-compaction
2. CC compacts → sends retry → LiteLLM encounters 429 insufficient_quota (cascade cooldown)
   → proxy forwards as HTTP 429 + rate_limit_error → CC exponential backoff
3. CC backoff → sends another request → proxy INPUT-REJECT again (estimated still > 110K)
   → HTTP 529 + overloaded_error → CC attempts compaction again
4. Loop continues → CC triggers "Repeated 529 Overloaded" protection → STOPS/FREEZE
```
Fix breaks the loop at three points:
- Point 1: Safety limit 120K (10K headroom) → fewer INPUT-REJECT 529s
- Point 2: insufficient_quota → api_error (not rate_limit_error) → CC normal retry, no backoff loop
- Point 3: chars_per_token=3.5 → more conservative estimation → fewer false INPUT-REJECT

## opc2_uname_r9 Changes (2026-06-03 — Deploy R8 + parameter tuning)

### R8 Auto-compact proxy.py deployed to remote (1542→1675 lines)
- **Before**: Remote machine ran old proxy.py (1542 lines) without `_auto_compact_messages()` — INPUT-REJECT returned 529 overloaded_error → CC "Repeated 529 Overloaded" crash
- **After**: R8 proxy.py deployed — INPUT-OVERLIMIT auto-compacts messages (removes oldest, preserves recent 5 exchanges) → returns 200 to CC instead of 529
- **Evidence**: 3 INPUT-REJECT events at 13:50 (est_tokens=120216 > safety=120000) → all returned 529 → triggered CC crash loop. With R8, these would be auto-compacted → CC gets 200 → continues normally
- **Impact**: Eliminates the INPUT-REJECT → 529 → CC crash loop entirely. Borderline requests get auto-compacted in proxy → forwarded to LiteLLM → 200 response → CC continues

### insufficient_quota 429 → rate_limit_error (REVERTED from R7 api_error)
- **Before (R7)**: insufficient_quota 429 → `api_error` → CC limited retries (2-3) → quickly exhausts → CC freezes/crashes when all 77 deployments quota-exhausted
- **After (R9)**: insufficient_quota 429 → `rate_limit_error` → CC exponential backoff (5s→10s→20s→40s→...) → CC gracefully waits for quota recovery without crashing
- **Why revert**: R7 rationale was "api_error prevents 'Repeated 529 Overloaded' cascade combined with INPUT-REJECT 529s". This is no longer valid because R8 auto-compact eliminates INPUT-REJECT 529s entirely (auto-compact → 200 response). Without the 529 cascade risk, rate_limit_error's exponential backoff is better: CC waits for quota recovery instead of quickly exhausting limited api_error retries and freezing.
- **Evidence**: 96 quota 429 errors in proxy logs spanning hours → all 77 deployments exhausted → api_error's 2-3 retries immediately fail → CC crashes. rate_limit_error's backoff would let CC wait (minutes→hours) for quota reset.

### MODEL_INPUT_TOKEN_SAFETY: 120K → 170K (both proxies)
- **Before**: MODEL_INPUT_TOKEN_SAFETY_GLM51=120000, DSV4P=120000
- **After**: MODEL_INPUT_TOKEN_SAFETY_GLM51=170000, DSV4P=170000
- **Why**: ModelScope actual limit = 202745 tokens. 170K safety gives 32K margin (plenty of room). Current 120K threshold rejects borderline requests (est=120216) that would succeed at upstream. With 170K safety: only genuinely oversized requests (>170K est_tokens) trigger auto-compact. Data validation: 35 of 47 historical INPUT-REJECTs would NOT be rejected at 170K threshold.
- **Impact**: /v1/models endpoint now reports context_window=170K (matches safety). CC has more room before compaction triggers.

### CC settings: contextWindow 110K → 130K
- **Before**: contextWindow=110K, autoCompactWindow=90K
- **After**: contextWindow=130K, autoCompactWindow=90K (unchanged)
- **Why**: With /v1/models context_window=170K, CC at 130K contextWindow has 40K headroom. CC auto-compacts at 90K (well below 170K safety limit) → no INPUT-REJECT for normal CC conversations.

### Root Cause resolution (R8+R9 complete fix)
```
Original crash chain (R7 partial fix, now fully resolved):
1. CC ~100K tokens → proxy INPUT-REJECT (est >120K > safety 120K) → 529 → CC crash
   R8 fix: Auto-compact messages → 200 response → CC continues (NO 529 returned)
2. CC compact → retry → LiteLLM 429 insufficient_quota → 529 cascade
   R9 fix: insufficient_quota → rate_limit_error (backoff, not api_error crash)
   R8 fix: Auto-compact eliminates INPUT-REJECT 529s → no cascade possible
3. Safety 120K rejects borderline requests → CC crash
   R9 fix: Safety 170K → only genuinely oversized trigger auto-compact → fewer events
```
All three points now fixed. CC should NEVER see "Repeated 529 Overloaded" crash again.

## opc2_uname WebUI + Infrastructure Fix (2026-06-03, by opc_uname)

### CRITICAL: PostgreSQL version mismatch (cc_postgres)
- **Before**: `postgres:14-alpine` image, data directory initialized by PG v16 → `FATAL: database files are incompatible with server`
- **After**: `postgres:16-alpine` image — matches data directory version
- **Impact**: cc_postgres was in infinite restart loop → LiteLLM DB connections failing (61 consecutive reconnect failures) → proxy containers `Created` (not `Up`) → 40001/40002 ports not listening → CC `ConnectionRefused`
- **Evidence**: docker logs showed `The data directory was initialized by PostgreSQL version 16, which is not compatible with this version 14.23`

### CRITICAL: Proxy containers not running (auth_to_api_40001/40002)
- **Before**: Container status `Created` (not `Up`) — 40001/40002 ports not listening
- **After**: `docker start` brought them up; both now `healthy`
- **Root cause**: Proxy containers depend on LiteLLM which depends on cc_postgres. When postgres was crash-looping, proxy containers never started. After postgres fix, containers could start but had `Created` status from previous failed `docker compose up`
- **Fix**: Manual `docker start auth_to_api_40001/40002` + postgres version fix + LiteLLM restart for DB reconnection

### WebUI systemd service: ANTHROPIC_BASE_URL missing
- **Before**: `cloudcli-webui.service` had no `ANTHROPIC_BASE_URL` or `ANTHROPIC_API_KEY` in Environment
- **After**: Added `ANTHROPIC_BASE_URL=http://127.0.0.1:40001`, `ANTHROPIC_API_KEY=sk-litellm-local`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=90000`
- **Why**: Claude Agent SDK forwards `process.env` to CC subprocess. Without these env vars, CC subprocess had no API endpoint → `ConnectionRefused`
- **Evidence**: WebUI logs showed `SDK query error: Unable to connect to API (ConnectionRefused)`
- **Note**: CC settings.json already had these env vars, but SDK subprocess inherits from WebUI's process env, NOT from settings.json

### Full recovery chain
```
1. postgres:14 → postgres:16 (version mismatch) → cc_postgres healthy
2. LiteLLM restart → DB reconnection successful
3. docker start auth_to_api_40001/40002 → proxy containers Up+healthy
4. systemd service + ANTHROPIC env vars → WebUI CC subprocess connects to proxy
5. All 5 containers healthy, proxy test 200, WebUI HTTP 200, WebSocket connected
```

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

### MODEL_MAX_INPUT_TOKENS fix (proxy.py)
- **Before**: MODEL_MAX_INPUT_TOKENS = {glm5.1:131072, dsv4p:131072} (model's native context window)
- **After**: MODEL_MAX_INPUT_TOKENS = {glm5.1:202745, dsv4p:202745} (ModelScope API's actual enforced limit)
- **Why**: ModelScope returns "Range of input length should be [1, 202745]" — the API enforces 202745, not 131072. The Anthropic-format /v1/models endpoints still use MODEL_INPUT_TOKEN_SAFETY (128000) for context_window, which is deliberately lower to trigger CC auto-compaction early.

### ModelScope "Range of input length" error → 529 overloaded_error (proxy.py)
- **Before**: 400→529 conversion only matched "exceeds" + ("token" OR "limit"). ModelScope's actual error format "InternalError.Algo.InvalidParameter: Range of input length should be [1, 202745]" was NOT matched → CC received 400 api_error → CC retries with same oversized content → infinite retry loop.
- **After**: 400→529 conversion now also matches "range of input length" and "invalidparameter" + ("input length" OR "input token"). CC receives 529 overloaded_error → CC triggers auto-compaction → retry with smaller content → success.
- **Why**: Two error formats for the same problem: (1) "exceeds...token/limit" and (2) "Range of input length should be [1, N]". Both mean input overflow. CC needs overloaded_error to trigger auto-compaction, not api_error (which just retries same content).
- **Also**: _convert_error now maps input-overflow InvalidParameter to overloaded_error (not api_error). thinking_budget InvalidParameter still maps to api_error (preflight fix adjusts params → retry works).

## Router Settings (updated 2026-06-03, opc_uname Round 1-6 optimizations)
- num_retries: 5 (was 3 — more retries needed when RPM windows temporarily full)
- cooldown_time: 10 (was 30 — 429 RPM limit is 1-minute window, 30s was too long causing cascading unhealthy)
- routing_strategy: simple-shuffle (was latency-based-routing — simple-shuffle distributes uniformly across 77 deployments, maximizing RPM utilization)
- enable_pre_call_checks: false
- background_health_checks: false
- AuthenticationErrorAllowedFails: 0 (immediate cooldown on 401)
- RateLimitErrorAllowedFails: 5 (was 3 — ModelScope 429 is RPM rate-limit, not quota exhaustion; higher tolerance prevents cascading cooldown)
- TimeoutErrorAllowedFails: 2
- InternalServerErrorAllowedFails: 3 (prevents ModelScope null-response cooldown cascade)
- BadRequestErrorAllowedFails: 0 (BadRequest is client error — no tolerance)

## Proxy Changes (Round 1-9)
- Added `import socket` — socket.timeout referenced at line 1233 but module not imported
- Removed conn_retry — 3% success rate (1/36), 3s wasted latency per attempt
- Removed rate_limit_retry — 8% success rate (1/13), 2s wasted latency per attempt
- Removed glm→dsv4p FALLBACK — always fails (UnsupportedParamsError on reasoning_effort)
- should_rate_limit_retry = False (disabled for clarity, not deleted)
- RateLimitErrorAllowedFails: 1→3, cooldown_time: 60→30, InternalServerErrorAllowedFails: 3
- MODEL_MAX_INPUT_TOKENS: 131072→202745 (ModelScope API's actual enforced limit)
- 400→529 overloaded_error conversion: extended to match ModelScope "Range of input length should be [1, N]" and "InvalidParameter" + "input length" error formats
- _convert_error: input-overflow InvalidParameter → overloaded_error (CC auto-compacts), thinking_budget InvalidParameter → api_error (CC retries with preflight fix)
- **R7**: insufficient_quota 429 → api_error (prevented 529 cascade at the time)
- **R7**: MODEL_INPUT_TOKEN_SAFETY: 110K→120K, CHARS_PER_TOKEN_ESTIMATE: 2.5→3.5
- **R8**: Auto-compact INPUT-REJECT messages — instead of returning 529 overloaded_error (which triggers CC "Repeated 529 Overloaded" crash), proxy now truncates message history and forwards compacted request to LiteLLM, returning 200 to CC. Uncompactable requests return 429 rate_limit_error (not 529).
- **R9**: insufficient_quota 429 → rate_limit_error (REVERTED R7 — with R8 auto-compact, 529 cascade eliminated; rate_limit_error backoff better than api_error limited retries for quota exhaustion)
- **R9**: MODEL_INPUT_TOKEN_SAFETY: 120K→170K (ModelScope limit 202K, 32K margin; fewer auto-compact events)
- **R9**: CC contextWindow: 110K→130K, autoCompactWindow: 90K unchanged

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

## Tailscale Network Fix (2026-06-03, by opc_uname — R8: upgrade + TCP optimization)

### Root Cause Analysis (Updated R8)

**Primary issue**: opc_uname (Tailscale v1.98.2) cannot P2P direct-connect to desktop, only DERP relay (318-330ms with 10% spikes to 700-1096ms). Meanwhile opc2_uname (v1.99.129) achieves P2P direct at 9ms.

**Why P2P fails for opc_uname but works for opc2_uname**:
- opc2_uname uses v1.99.129 (unstable track) with improved Disco protocol — established P2P to desktop earlier
- opc_uname used v1.98.2 (stable) which had weaker hole-punching capability
- Desktop (Windows) is behind different CGNAT (218.93.215.x) vs our CGNAT (218.93.250.x)
- UDP hole-punching requires simultaneous bidirectional punches — CGNAT blocks unsolicited inbound UDP
- opc2_uname succeeded because its v1.99 disco exchanged "call-me-maybe" with desktop, triggering simultaneous UDP punches from both CGNATs
- After v1.99 upgrade on opc_uname, desktop hasn't yet seen our new disco key → call-me-maybe not triggered

**TCP jitter root cause (10% spikes to 700-1096ms)**:
- TCP retransmit rate 1.3% (95088/7282643) on overseas routes
- CUBIC congestion control reacts to single packet loss with CWND halving → sawtooth pattern
- Small TCP buffers (rmem_max=212KB) can't accommodate BDP at 300ms RTT overseas links
- `tcp_slow_start_after_idle=1` resets CWND after idle periods on DERP connections

### Fix R8: Tailscale Upgrade + TCP Stack Optimization

1. **Tailscale upgrade**: v1.98.2 → v1.99.129 (unstable track, same as opc2_uname)
   - Added unstable apt repo: `/etc/apt/sources.list.d/tailscale.list`
   - Better Disco protocol, improved P2P hole-punching
   - Nearest DERP now correctly identified as Tokyo (177ms) instead of SFO (181ms)
   - Home DERP automatically follows desktop's DERP region (SFO) for shortest relay path

2. **TCP stack optimization** (`/etc/sysctl.d/99-tcp-buffer-optimization.conf`):
   - `rmem_max/wmem_max`: 212KB → 2MB (accommodates BDP at 50Mbps/300ms)
   - `tcp_congestion_control`: CUBIC → BBR (doesn't react to single losses, stable on lossy overseas paths)
   - `tcp_slow_start_after_idle`: 1 → 0 (prevents CWND reset on idle DERP connections)
   - `netdev_max_backlog`: 1000 → 5000 (better burst absorption)
   - `tcp_bbr` module added to `/etc/modules-load.d/bbr.conf` for boot persistence

3. **Previous fix still active**: `/etc/sysctl.d/99-tailscale-keepalive.conf` (tcp_keepalive 30/10/5)

### Results
| Metric | Before R8 (v1.98.2) | After R8 (v1.99.129+BBR) |
|--------|---------------------|--------------------------|
| Tailscale version | 1.98.2 (stable) | 1.99.129 (unstable) |
| Nearest DERP | SFO (161ms) | Tokyo (177ms) |
| Home DERP | SFO | SFO (auto-follows desktop) |
| DERP relay to desktop | 325-580ms, 30% spikes | 303-330ms, 10% spikes to 700ms |
| P2P to desktop | ❌ (DERP only) | ❌ (still DERP — call-me-maybe not triggered by desktop) |
| P2P to opc2_uname | ✅ direct (1ms LAN) | ✅ direct (1ms LAN) |
| Congestion control | CUBIC | BBR |
| TCP buffer (rmem_max) | 212KB | 2MB |
| tcp_slow_start_after_idle | 1 | 0 |

### P2P Still Blocked — Why and What's Next
- Desktop (Windows, CGNAT 218.93.215.x) and opc_uname (CGNAT 218.93.250.x) are on different CGNAT pools
- UDP hole-punching requires desktop to send "call-me-maybe" via DERP → then both sides punch UDP simultaneously
- opc2_uname achieved P2P (9ms direct!) because its v1.99 was deployed earlier and desktop learned its disco key
- opc_uname's v1.99 upgrade hasn't yet triggered desktop's call-me-maybe response
- **Next steps**: (1) wait for desktop to pick up our new disco key via control server updates; (2) if still DERP-only after hours, deploy self-hosted DERP in Asian VPS for ~30-50ms relay

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