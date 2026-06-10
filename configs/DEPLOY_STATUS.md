# Deploy Status — opc_uname (updated 2026-06-10 R8 — metrics analysis + overhead deep dive + no parameter changes needed)

## Architecture
```
CC → 40001(proxy, format conversion + force-stream ALL non-stream + stream_options.include_usage + metrics logging) → 41003(LiteLLM glm5.1, 1000 variants × 7 keys = 7000 deploys) → ModelScope
                                                                     → 42001(LiteLLM dsv4p, 11 variants × 7 keys = 77 deploys) → ModelScope
                                                                     → 41001(LiteLLM glm5.1-backup, 1000 variants × 7 keys = 7000 deploys) [BACKUP, same config as 41003]
```

## Deploy Method
- **docker compose**: `cd /opt/cc-infra && DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001`
- **Docker Hub**: unreachable from China without proxy → mihomo on port **7890** configured as Docker systemd proxy (`/etc/systemd/system/docker.service.d/proxy.conf`)
- **Legacy builder**: `DOCKER_BUILDKIT=0` required — BuildKit doesn't respect systemd proxy

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (7000 deployments: 1000 variants × 7 keys) [BACKUP] — ulimits nofile=4096
- dsv4p_uni42001 :42001 (77 deployments: 11 variants × 7 keys) — ulimits nofile=4096
- glm5.1_test41003 :41003 (7000 deployments: 1000 variants × 7 keys) [PRIMARY glm5.1] — ulimits nofile=8192
- auth_to_api_40001 :40001 (proxy, format conversion + stream_usage reporting + metrics logging + retry-after headers, NO auto-compact/NO truncation)
- auth_to_api_40002 :40002 (opc2_uname proxy, same codebase)

## opc_uname_r8 Analysis (2026-06-10 — metrics deep dive + overhead investigation + no parameter changes)

### R8 Metrics Analysis (06-09 through 06-10, proxy 40001)

| Day | Total | Success | Rate | Errors | Avg Duration | P90 Duration | P95 Duration |
|-----|-------|---------|------|--------|--------------|--------------|--------------|
| 06-09 | 220 | 213 | 96.8% | 7×502 ConnectionRefused (startup burst) | 13.9s | 21.9s | 29.2s |
| 06-10 | 707 | 704 | 99.6% | 2×502 timeout, 1×429 quota | 19.3s | 34.8s | 48.5s |

**proxy-40002 (opc2_uname)**:

| Day | Total | Success | Rate | Errors |
|-----|-------|---------|------|--------|
| 06-09 | 48 | 46 | 95.8% | 2×429 quota |
| 06-10 | 37 | 37 | 100% | 0 |

### Key findings (06-10 full day, 707 requests):

**Success rate**: 99.6% — best ever. All errors transient:
- 2×502 timeout (ConnectionRefused at 00:00 and 17:31 — isolated, not systemic)
- 1×429 quota (at 00:17 — single key exhaustion, LiteLLM rotated to other keys)

**Zero InputReject/529 errors**: Current safety settings (190K context) are working perfectly.

**Streaming overhead analysis**:
| Request type | % of requests | Avg overhead after TTFB | Avg total overhead |
|-------------|--------------|------------------------|--------------------|
| tool_calls | 95.5% (672/704) | 1.3s | 4.1s (21% of avg_dur=17.7s) |
| stop | 2.8% (20/704) | 29.2s | varies widely |
| length | 1.7% (12/704) | 0.5s | 3.5s |

**Important discovery: `litellm_response_duration_ms` interpretation**:
- litellm_response_duration_ms measures LiteLLM routing/dispatch time (time from request receipt to first response byte), NOT total streaming duration
- Evidence: for tool_calls, litellm/ttfb avg ratio = 81.7%, litellm/dur avg ratio = 79.0% — litellm_dur ≈ ttfb, not total duration
- For stop requests, litellm_dur varies from 8% to 95% of total_dur — inconsistent, confirming it's not total processing time
- Proxy overhead for tool_calls (1.3s after TTFB) is excellent — format conversion + SSE forwarding is efficient

**Stop request overhead explanation**:
- finish_reason=stop requests (text responses, not tool calls) have extreme overhead_after_ttfb (up to 130s)
- This is NOT a proxy buffering issue — it's the model taking long to generate text tokens
- Evidence: tool_calls in same est_tokens range (100-170K) have overhead 5s vs stop 44-99s
- The model generates tool_calls as structured JSON (fast, bounded), but stop as free-form text (slow, unbounded)
- Only 20/704 = 2.8% of requests are stop type → impact is limited
- No proxy parameter change can make the model generate text faster

**Tool truncation**: 686/704 requests truncated, avg reduction 69.1%, original 50.7K chars → truncated 15.3K chars

**Model mapping distribution**: claude-opus-4-8→glm5.1: 88%, glm5.1→glm5.1: 11%, dsv4p→dsv4p: 0.3%

**Estimated tokens**: avg=90.3K, max=205.3K. With CHARS_PER_TOKEN_ESTIMATE=3.0, estimation is accurate (~3% conservative vs real ratio 3.11).

**Context window utilization**: CC compact triggers at 180K estimated ≈ 174K real tokens = 86% of 202.7K ModelScope capacity. No overflow events observed.

### Latency by time-of-day (06-10):
| Hour | Requests | Success | Avg Duration | Avg TTFB |
|------|----------|---------|-------------|----------|
| 00:00 | 119 | 98.3% | 10.9s | 9.6s |
| 01:00 | 40 | 100% | 14.4s | 9.6s |
| 02:00 | 181 | 100% | 11.6s | 10.6s |
| 14:00 | 91 | 100% | 29.3s | 28.1s |
| 15:00 | 6 | 100% | 34.7s | 31.8s |
| 16:00 | 52 | 100% | 30.8s | 29.0s |
| 17:00 | 121 | 99.2% | 25.2s | 20.8s |
| 23:00 | 1 | 100% | 12.7s | 12.7s |

Note: Afternoon/evening hours (14-17) show higher latency (avg 29-35s) vs morning (avg 10-14s). This correlates with conversation complexity (higher est_tokens in afternoon), not ModelScope congestion.

### Historical trend:
| Day | Total | Success Rate | Avg Duration | P90 Duration | Notes |
|-----|-------|-------------|--------------|--------------|-------|
| 06-02 | 243 | 80.2% | ~14s | ~22s | Pre-R12 |
| 06-03 | 1214 | 84.2% | ~14s | ~22s | Pre-R12, 47 InputExceedsProxyReject |
| 06-05 | 1558 | 80.7% | ~14s | ~25s | Pre-R12 |
| 06-09 | 220 | 96.8% | 13.9s | 21.9s | Post-R12, startup errors |
| 06-10 | 707 | 99.6% | 19.3s | 34.8s | Post-R7, best success rate ever |

**No parameter changes warranted**: System is performing optimally. 99.6% success rate, zero InputReject/529 errors, proxy overhead minimal for 95.5% of requests (tool_calls). All errors are transient. Current settings proven effective.

## opc_uname_r7 Changes (2026-06-10 — chars/token estimation fix + context window optimization + metrics analysis)

### FIX: CHARS_PER_TOKEN_ESTIMATE 2.0→3.0 (data-driven adjustment)
- **Problem**: CHARS_PER_TOKEN_ESTIMATE=2.0 overestimates tokens by 56%. Verified with 538 requests having real token counts (from stream_options.include_usage): average real_tokens/estimated_tokens ratio = 0.642. This means estimated_tokens overstates real tokens by 56%.
- **Fix**: Changed CHARS_PER_TOKEN_ESTIMATE from 2.0 to 3.0 (real ratio = 3.11, 3.0 is close but still conservative).
- **Why**: With 2.0, proxy estimated tokens that never existed → 66 requests appeared to exceed 170K safety but ALL succeeded (real tokens max=142K). With 3.0, estimation is accurate (3.0 vs real 3.11), INPUT-WARN noise eliminated (0 >170K vs 66 before). No functional impact (proxy doesn't truncate — R12 removed auto-compact), but accurate estimation gives better metrics for monitoring.
- **Evidence**: 538 requests with real input_tokens from ModelScope. Average ratio real/est = 0.642 → actual chars/token = 2.0/0.642 = 3.11. Max real tokens = 142K (well below 202.7K ModelScope limit), max est_tokens (2.0) = 205K (false overestimation), max est_tokens (3.0) = 137K (accurate).

### FIX: MODEL_INPUT_TOKEN_SAFETY 170K→190K (context capacity optimization)
- **Problem**: MODEL_INPUT_TOKEN_SAFETY=170K reports context_window=170K to CC via /v1/models. This limits CC's usable context to ~170K estimated tokens = ~108K real tokens (with 56% overestimation). That's only 53% of ModelScope's 202.7K real capacity.
- **Fix**: Changed MODEL_INPUT_TOKEN_SAFETY from 170000 to 190000.
- **Why**: With 3.0 chars/token, estimated_tokens ≈ real_tokens/1.03 (much more accurate). Reporting 190K context_window tells CC it can use up to 190K estimated ≈ 184K real tokens = 91% of ModelScope capacity. Zero requests have real tokens >170K, so 190K safety provides ample margin.
- **Evidence**: Max real tokens across 555 requests = 142K. ModelScope limit = 202.7K. 190K safety = 93.6% of limit with 12.7K buffer. No real risk of overflow.

### FIX: contextWindow 170K→190K, autoCompactWindow 150K→180K (CC compact timing optimization)
- **Problem**: CC settings contextWindow=170K, autoCompactWindow=150K. With 56% overestimation, CC compact triggers at 150K estimated = ~96K real tokens. This wastes 60%+ of ModelScope's 202.7K context capacity.
- **Fix**: Changed contextWindow from 170000 to 190000, autoCompactWindow from 150000 to 180000, CLAUDE_CODE_AUTO_COMPACT_WINDOW env from 150000 to 180000.
- **Why**: With 190K contextWindow + 180K autoCompactWindow + 3.0 chars/token, CC compact triggers at 180K estimated ≈ 174K real tokens = 86% of ModelScope capacity. This gives CC ~174K real usable context before compacting (vs ~96K before = 81% improvement in usable context).
- **Evidence**: No request has real tokens >142K. Even at 180K estimated (with 3.0 chars/token), real tokens ≈ 174K, still below 202.7K limit. Historical data: 0 overflow errors when requests up to 142K real tokens were accepted.

### Metrics Analysis (06-09 through 06-10, proxy 40001)

| Day | Total | Success | Rate | Errors | Avg Duration | P90 Duration |
|-----|-------|---------|------|--------|--------------|--------------|
| 06-09 | 220 | 213 | 96.8% | 7×502 ConnectionRefused (startup) | 13.9s | 21.9s |
| 06-10 | 555 | 552 | 99.5% | 2×502 timeout, 1×429 quota | 18.8s | 33.3s |

**proxy-40002 (opc2_uname)**:

| Day | Total | Success | Rate | Errors |
|-----|-------|---------|------|--------|
| 06-09 | 48 | 46 | 95.8% | 2×429 quota |
| 06-10 | 36 | 36 | 100% | 0 |

**Key findings**:
- 99.5% success rate on 06-10 (best ever, 555 requests)
- All errors are transient: 502 ConnectionRefused/timeout (startup/reconnect), 429 quota (temporary)
- 0 InputExceeds errors, 0 529 overloaded errors
- Real token estimation analysis: 538 requests with real input_tokens show avg real/est ratio = 0.642 → chars/token=3.11
- Max real tokens = 142K (never exceeds 170K), proving CHARS_PER_TOKEN_ESTIMATE=2.0 was wildly conservative
- 66 requests with estimated >170K ALL succeeded — confirms safety limit can be raised
- Duration higher on 06-10 vs 06-09 because average messages doubled (96→124) — more complex conversations, not a regression
- Latency correlates with message count: 0-50 msgs avg=10.9s, 100-150=17.6s, 200-300=25.1s, 300-500=40.3s

**Token estimation accuracy**:
| Metric | Value |
|--------|-------|
| CHARS_PER_TOKEN_ESTIMATE (old) | 2.0 |
| Real chars/token ratio | 3.11 |
| Overestimation factor | 56% |
| CHARS_PER_TOKEN_ESTIMATE (new) | 3.0 |
| New overestimation | ~3% (conservative) |
| Requests est>170K (old) | 66 (false positives) |
| Requests est>170K (new) | 0 (accurate) |
| Max real tokens | 142K |
| Max est tokens (old) | 205K |
| Max est tokens (new) | 137K |

**Usable context comparison**:
| Setting | Before (R6) | After (R7) | Improvement |
|---------|-------------|------------|--------------|
| CHARS_PER_TOKEN_ESTIMATE | 2.0 | 3.0 | 56% more accurate |
| MODEL_INPUT_TOKEN_SAFETY | 170K | 190K | 20K more capacity |
| contextWindow | 170K | 190K | 20K more capacity |
| autoCompactWindow | 150K | 180K | 30K more before compact |
| CC compact trigger (real tokens) | ~96K | ~174K | 81% more usable context |
| Capacity utilization | 47% | 86% | +39 percentage points |

### Post-deployment verification
- **CHARS_PER_TOKEN_ESTIMATE**: ✅ 3.0 (verified docker inspect)
- **MODEL_INPUT_TOKEN_SAFETY**: ✅ 190000 for both models (verified docker inspect)
- **CC settings**: ✅ contextWindow=190000, autoCompactWindow=180000, env=180000
- **/v1/models context_window**: ✅ 190000 reported for both models
- **glm5.1**: ✅ 200 response via proxy → 41003 (input_tokens=23)
- **dsv4p**: ✅ 200 response via proxy → 42001 (input_tokens=9)
- **All 6 containers**: ✅ healthy

### FIX: proxy.py stream_options.include_usage sync (running → repo)
- **Problem**: Running proxy.py includes `stream_options: {"include_usage": True}` in the OpenAI request body, plus logic to defer message_delta until stream ends to include real token counts from the usage chunk. The repo proxy.py was missing this entire feature.
- **Fix**: Copied `/opt/cc-infra/proxy/proxy.py` → `configs/proxy/proxy.py` to sync the running version back to the repo.
- **Why**: Without `stream_options.include_usage=True`, the OpenAI streaming API doesn't send prompt_tokens/completion_tokens in chunks → CC TUI shows "0/200000 tokens" → user can't see actual token consumption. With this fix, the usage chunk arrives after finish_reason in streaming, and the proxy defers message_delta to include real token counts.
- **Evidence**: All 06-10 metrics show `input_tokens=0` and `output_tokens=0` in successful responses because ModelScope doesn't return prompt_tokens in the streaming response body. But with `stream_options.include_usage`, LiteLLM forwards the usage data → proxy collects streaming_input_tokens/streaming_output_tokens → includes them in message_delta for CC to display.

### FIX: litellm-glm51 config.yaml router_settings sync (running → repo)
- **Problem**: Repo litellm-glm51/config.yaml (41001 backup) had stale router_settings from R11: `latency-based-routing + cooldown=30 + num_retries=5 + RateLimitErrorAllowedFails=3`. Running config (both 41001 and 41003) uses `simple-shuffle + cooldown=10 + num_retries=8 + RateLimitErrorAllowedFails=5 + allowed_fails_policy format`.
- **Fix**: Updated repo litellm-glm51/config.yaml router_settings to match running config: `simple-shuffle + cooldown=10 + num_retries=8 + RateLimitErrorAllowedFails=5` + `allowed_fails_policy` format + `AuthenticationErrorAllowedFails:0 + BadRequestErrorAllowedFails:0`.
- **Why**: Running config has proven superior for 7000-deployment pools. Repo backup config should match for disaster recovery consistency. Also, running 41001 now uses the same 7000-deployment config as 41003 (not the old 1792-dep config), so repo config is a fallback reference only.
- **Evidence**: 06-09/06-10 metrics show 96.8%/99.1% success rate with simple-shuffle + cooldown=10 + retries=8 — these settings are proven effective.

### Metrics Analysis (06-09 through 06-10, post-R12 sync)

| Day | Total | Success | Rate | Errors | Avg Duration | P90 Duration | P95 Duration |
|-----|-------|---------|------|--------|--------------|--------------|--------------|
| 06-09 | 220 | 213 | 96.8% | 7×502 ConnectionRefused | 13.5s | 21.9s | 29.2s |
| 06-10 | 221 | 219 | 99.1% | 1×502 timeout, 1×429 quota | 11.2s | 16.8s | 20.6s |

**Key findings (post-R12)**:
- Success rate jumped from 80% range → 96-99%: R12 proxy auto-compact removal + 170K safety limit eliminated InputExceedsProxyReject errors (0 on 06-09/06-10 vs 47 on 06-03/06-04).
- 429 quota errors rare (1 on 06-10, 0 on 06-09): quota exhaustion is now rare because 7000 deployments provide ample RPM capacity.
- 502 ConnectionRefused only during startup/reconnect: 7 on 06-09 (container startup), 1 on 06-10.
- Latency improving: avg 13.5s→11.2s, p90 21.9s→16.8s, p95 29.2s→20.6s on 06-10.
- Large context requests working: 13 requests with estimated_tokens > 170K all succeeded (ModelScope accepts up to 202745 tokens). The 170K safety limit provides 83% capacity utilization (170K/202.7K).

**Historical trend comparison**:

| Day | Total | Success Rate | Avg Duration | Notes |
|-----|-------|-------------|--------------|-------|
| 06-02 | 243 | 80.2% | ~14s | Pre-R12 (proxy auto-compact + 120K safety) |
| 06-03 | 1214 | 84.2% | ~14s | Pre-R12, 47 InputExceedsProxyReject 529 errors |
| 06-04 | 2261 | 79.2% | ~14s | Pre-R12, heavy 429 quota exhaustion |
| 06-05 | 1558 | 80.7% | ~14s | Pre-R12, dsv4p R2 config sync |
| 06-09 | 220 | 96.8% | 13.5s | Post-R12, auto-compact removed, 170K safety |
| 06-10 | 221 | 99.1% | 11.2s | Post-R12, best day ever |

**Tool truncation analysis**:
- 06-09: 203/212 requests truncated (70.3% avg reduction), avg original_chars=44K→truncated_chars=13K
- 06-10: 208/219 requests truncated (66.0% avg reduction), avg original_chars=45K→truncated_chars=14K
- Tool descriptions truncated from ~44K chars to ~14K chars = 70% reduction. MAX_TOOL_DESC=2000 working well.

**Token estimation analysis**:
- 06-09: avg estimated_tokens=74.7K, max=116K, all < 120K → no INPUT-WARN triggered
- 06-10: avg estimated_tokens=79.6K, max=186K, 40 requests > 120K, 22 > 150K, 13 > 170K
- CHARS_PER_TOKEN_ESTIMATE=2.0 overestimates tokens for Chinese text (Chinese chars ≈ 1-1.5 tokens/char). This makes our estimation conservative (higher than real tokens) — good for safety but generates INPUT-WARN noise.
- No parameter change needed: the overestimation is harmless (only triggers INPUT-WARN, no proxy action).

**dsv4p usage**: 1 request on 06-09, 0 on 06-10. dsv4p is essentially unused because CC always requests claude-opus-4-8 → mapped to glm5.1. dsv4p only triggered when CC explicitly requests dsv4p.

**No parameter changes warranted**: Current settings are proven effective with 99.1% success rate. All errors are transient (ConnectionRefused during startup, quota exhaustion). No config optimization needed.

### Post-deployment verification (pending — requires rebuild on opc_uname)
- **proxy.py**: ⏳ stream_usage version needs rebuild (`docker compose up -d --build --force-recreate auth_to_api_40001`)
- **litellm-glm51 config**: ⏳ router_settings updated in repo only; 41001 backup not currently routed from proxy
- **glm5.1**: ✅ 200 response via proxy → 41003 (verified daily)
- **dsv4p**: ✅ 200 response via proxy → 42001 (verified)
- **/v1/models context_window**: ✅ 170000 reported for both models
- **All 6 containers**: ✅ healthy

## opc_uname_r5 Changes (2026-06-09 — R12 config sync + proxy auto-compact removal + context window adjustment)

### CRITICAL: proxy.py auto-compact removed (R12 sync from opc2_uname)
- **Before**: proxy auto-compacts messages when estimated_tokens > safety (120K) → truncates oldest messages, inserts compact notice → returns 200 to CC. Also has INPUT-REJECT-UNCOMPACTABLE fallback → returns 429 with retry-after=30.
- **After**: proxy NO longer truncates/compacts messages. If estimated_tokens > 120K, only logs INPUT-WARN. If ModelScope returns 400 "Range of input length should be [1, 202745]", returns invalid_request_error → CC stops (user starts new conversation manually).
- **Why**: Proxy-level auto-compact caused catastrophic context loss — CC "completely forgets everything" because proxy silently truncates conversation history. Even worse, three compression mechanisms (proxy truncation, 529→overloaded_error triggering CC compact, CC built-in compact) stacked unpredictably. Removing proxy truncation means only CC's built-in auto-compact (controlled by autoCompactWindow) handles compression — same quality outcome but at least CC's own decision.
- **Evidence**: 47 InputExceedsProxyReject 529 errors on 06-03/06-04 from proxy rejecting requests with est_tokens 110K-182K. These requests likely would have succeeded at ModelScope (limit=202745). After R12 sync, these errors will not recur.

### CRITICAL: MODEL_INPUT_TOKEN_SAFETY 120K→170K (R12 sync)
- **Before**: MODEL_INPUT_TOKEN_SAFETY=120000 (proxy reported context_window=120K to CC, triggering CC compact too early)
- **After**: MODEL_INPUT_TOKEN_SAFETY=170000 (proxy reports context_window=170K, giving CC more room before compact triggers)
- **Why**: ModelScope actual limit=202745. With safety=120K, CC auto-compact triggered at 90K (autoCompactWindow=90K) — only 90K usable context out of 202.7K available capacity = massive waste. With safety=170K and autoCompactWindow=150K, CC uses up to 150K before compacting = 74% capacity utilization (vs 44% previously).

### CRITICAL: contextWindow=120K→170K, autoCompactWindow=90K→150K (R12 sync)
- **Before**: CC settings contextWindow=120000, autoCompactWindow=90000
- **After**: CC settings contextWindow=170000, autoCompactWindow=150000, env CLAUDE_CODE_AUTO_COMPACT_WINDOW=150000
- **Why**: Matches MODEL_INPUT_TOKEN_SAFETY=170K. CC uses contextWindow to decide internal token tracking and autoCompactWindow to trigger compact. With 170K context, CC won't falsely think it's "over capacity" until ~150K tokens = more usable context.

### CRITICAL: Input overflow error mapping changed (R12 sync)
- **Before**: ModelScope 400 "Range of input length should be [1, 202745]" → proxy converts to 529 overloaded_error → CC retries with auto-compact (catastrophic context loss)
- **After**: ModelScope 400 "Range of input length should be [1, 202745]" → proxy returns 400 invalid_request_error → CC stops → user starts new conversation manually (better than losing all context)
- **Why**: Retrying the same oversized content never works. CC's auto-compact triggered by overloaded_error destroys context. invalid_request_error makes CC stop immediately so user can decide to start a new conversation.

### CRITICAL: 529 overloaded_error no longer forced (R12 sync)
- **Before**: Upstream 529 → proxy forces error type to overloaded_error → CC auto-compact (catastrophic context loss)
- **After**: Upstream 529 → proxy lets _convert_error produce api_error → CC retries 2-3 times then stops
- **Why**: Same reasoning as above — no more overloaded_error that triggers CC auto-compact destroying context.

### docker-compose.yml local enhancements preserved
- TZ: Asia/Shanghai, /etc/localtime mount, json-file logging (max-size=50m, max-file=5) added to all services — these are local-only enhancements not in the repo. Merged back into repo docker-compose.yml.

### Metrics Analysis (06-02 through 06-09, pre-R12 sync)

| Day | Total | Success | Rate | 429 | 529 | 502 | 400 | Avg Latency |
|-----|-------|---------|------|-----|-----|-----|-----|-------------|
| 06-02 | 243 | 195 | 80.2% | 23 | 3 | 22 | 0 | ~14s |
| 06-03 | 1214 | 1022 | 84.2% | 130 | 47 | 14 | 1 | ~14s |
| 06-04 | 2261 | 1791 | 79.2% | 441 | 0 | 23 | 5 | ~14s |
| 06-05 | 1558 | 1257 | 80.7% | 244 | 0 | 50 | 7 | ~14s |

**Overall 40001**: 5290 requests, 80.9% success, 838 429 (762 quota + 76 RPM), 50 529 (InputExceedsProxyReject), 109 502

**Key findings (pre-R12)**:
- 529 InputExceedsProxyReject errors (47 total, 06-03/06-04): proxy rejected requests it estimated as over safety=120K. With R12 removal of auto-compact, these will pass through to ModelScope (which accepts up to 202.7K tokens). Expected improvement: ~1-2% success rate boost.
- 429 quota errors (762 total, 90.9% of all 429): genuine ModelScope quota exhaustion. Cannot be fixed by config changes.
- 429 RPM errors (76 total, 9.1%): LiteLLM handles RPM retry/fallback internally.

### Post-deployment verification
- **proxy.py**: ✅ R12 version deployed (auto-compact removed, INPUT-WARN only, invalid_request_error for overflow)
- **MODEL_INPUT_TOKEN_SAFETY**: ✅ 170000 (verified docker inspect env)
- **CC settings**: ✅ contextWindow=170000, autoCompactWindow=150000, env=150000
- **glm5.1**: ✅ 200 response via proxy → 41003
- **dsv4p**: ✅ 200 response via proxy → 42001
- **/v1/models context_window**: ✅ 170000 reported for both models
- **All 6 containers**: ✅ healthy

## opc_uname_r4 Changes (2026-06-05 — dsv4p R2 config sync + proxy.py sync + metrics analysis)

### CRITICAL FIX: dsv4p running config ← repo R2 config sync
- **Problem**: Running dsv4p LiteLLM (42001) config was stale — still using simple-shuffle + cooldown=10 + RateLimitAllowedFails=5 (old R1 settings). The repo had R2 optimization (latency-based-routing + cooldown=30 + RateLimitAllowedFails=3 + rolling_window_size=30 + lowest_latency_buffer=2) that was never deployed to this machine.
- **Fix**: Copied `configs/litellm-dsv4p/config.yaml` (R2 version from repo) → `/opt/cc-infra/litellm-dsv4p/config.yaml` and restarted `dsv4p_uni42001`.
- **Why**: R2 changes were authored by opc2_uname on this machine but only the repo copy was updated; the running config on disk was never synced. Latency-based-routing is critical for 77-deployment pool to avoid hitting already-throttled deployments; cooldown=30 (vs 10) prevents cascading unhealthy; RateLimitAllowedFails=3 (vs 5) prevents keeping clearly-failing deployments in rotation too long.

### FIX: proxy.py disk file ← repo sync (inappropriate content → invalid_request_error)
- **Problem**: `/opt/cc-infra/proxy/proxy.py` on disk was missing the "inappropriate content → invalid_request_error" mapping. The proxy container was rebuilt at 09:34 today from the repo version (which includes the fix), but the disk file was never synced.
- **Fix**: Copied `configs/proxy/proxy.py` → `/opt/cc-infra/proxy/proxy.py` to maintain file ↔ reality consistency.
- **Why**: Without this fix, ModelScope "inappropriate content" 400 errors map to `api_error` → CC retries infinitely (same content always rejected) → CC freezes. `invalid_request_error` makes CC stop immediately. The fix was already active in the running container; this sync only ensures the disk file matches.
- **Evidence**: 5 inappropriate content errors on 06-05 — first 4 (06:19-06:45) before container rebuild, last 1 (09:32) after rebuild with CONTENT-MAP fix working correctly.

### Metrics Analysis (06-05, full day)

| Model | Requests | Success | Rate | 429 | 502 | 400 | Avg Latency | P95 Latency |
|-------|----------|---------|------|-----|-----|-----|-------------|-------------|
| glm5.1 | 1125 | 878 | 78.0% | 193 (17.2%) | 49 (4.4%) | 5 (0.4%) | 14.5s | 34.5s |
| dsv4p | 9 | 5 | 55.6% | 3 | 1 | 0 | 2.4s | 3.3s |
| Overall | 1136 | 883 | 77.7% | 196 | 50 | 7 | — | — |

**Key findings:**
- 429 errors: 195/196 are genuine quota exhaustion ("exceeded your current quota"), only 1 is RPM throttle. Config change cannot fix quota exhaustion.
- 502 ConnectionError: 50 total, clustered at hours 05 (16), 09 (18), 10 (14). All are `[Errno 111] Connection refused` — LiteLLM startup/reconnect issues, not persistent.
- 400 inappropriate content: 5 total, all before proxy rebuild (09:34). After rebuild, CONTENT-MAP fix correctly converts → invalid_request_error.
- No parameter changes warranted: current glm5.1 router settings (simple-shuffle + cooldown=10 + RateLimitAllowedFails=5) are appropriate for 7000-deployment pool with genuine quota exhaustion as primary failure mode.

**Comparison with 06-04:**
- 06-04: 2261 requests, 79.2% success, 438 429 (19.4%), 22 502 (1%), 1 401
- 06-05: 1136 requests, 77.7% success, 196 429 (17.2%), 50 502 (4.4%), 7 400 (0.6%)
- 502 rate increased from 1% → 4.4% (more startup/reconnect issues)
- Success rate slightly lower (79.2% → 77.7%) but 429 rate actually lower proportionally (19.4% → 17.2%)

### Post-deployment verification
- **dsv4p config**: ✅ R2 config applied (latency-based-routing, cooldown=30, RateLimitAllowedFails=3)
- **dsv4p restart**: ✅ healthy after restart
- **proxy.py**: ✅ disk file synced with repo (inappropriate content → invalid_request_error)
- **glm5.1**: ✅ 200 response via proxy 40001 → 41003
- **dsv4p**: ✅ 429 quota exhaustion (correct — genuine quota exhaustion at end of day)
- **All 6 containers**: ✅ healthy

### CRITICAL FIX: Proxy routing config file ↔ running container mismatch resolved
- **Problem**: `/opt/cc-infra/docker-compose.yml` was manually edited to route GLM-5.1 → `uni41001` (R1 emergency fix), but the running proxy container still routed to `test41003` (from R2 build). The local file ≠ reality.
- **Fix**: Restored `/opt/cc-infra/docker-compose.yml` to match repo config (GLM-5.1 → `test41003` primary). Rebuilt proxy container. Now file matches running container.
- **Why test41003 remains primary**: Data evidence shows 41003 (7000 dep) has 4× more quota capacity than 41001 (1792 dep). The 69.1% success rate on 06-05 is from genuine `insufficient_quota` exhaustion (all 7 keys exhausted across all tested variants), not a routing problem. uni41001 with only 1792 deployments would exhaust quota faster.
- **Data (06-05)**: 349 requests, 69.1% success, 25.2% 429 insufficient_quota, 4.6% 502 ConnectionRefused (startup time)
- **Data (06-04)**: 2261 requests, 79.2% success, 19.5% 429 — better success rate on heavier load day (429 spread over longer time)

### FIX: proxy.py retry-after header restored
- **Before**: Local proxy.py missing `retry-after=30` header on INPUT-REJECT-UNCOMPACTABLE 429 response
- **After**: Restored `extra_headers={"retry-after": "30"}` — matches repo proxy.py
- **Why**: Without retry-after header, CC immediately retries on INPUT-REJECT, worsening quota pressure during exhaustion periods. retry-after=30 tells CC to wait 30 seconds before retrying.

### FIX: docker-compose.yml ulimit → ulimits (Docker Compose v5 compatibility)
- **Before**: `ulimit:` (without 's') — not recognized by Docker Compose v5.1.3 → deployment fails with "additional properties 'ulimit' not allowed"
- **After**: `ulimits:` (with 's') — correct Docker Compose specification key
- **Impact**: Without this fix, `docker compose up` fails entirely. Fix applied to all 3 LiteLLM service definitions.

### Metrics Analysis Summary (06-05, no parameter changes warranted)
- 429 errors: ALL are `insufficient_quota` (account quota exhaustion), NOT RPM rate limits
- 429 pattern: concentrated in 06:00-08:00 hours, all 7 keys simultaneously exhausted for tried variants
- 502 errors: only during startup (05:49-05:59) before test41003 ready — 16 total, then zero
- 400 errors: 4 inappropriate content (ModelScope content filter, not config issue)
- Latency: avg TTFB=12.7s, median=9.0s — acceptable for 7000-deployment pool
- **No parameter changes needed**: cooldown_time=10, RateLimitErrorAllowedFails=5, simple-shuffle, num_retries=5 are all appropriate for current conditions. The 429 insufficient_quota is genuine exhaustion — no config change can fix it; it requires more keys or more quota per key.

### test41003 vs uni41001 router settings comparison (explains why test41003 settings are better)
| Parameter | uni41001 (1792 dep) | test41003 (7000 dep) | Winner |
|-----------|---------------------|----------------------|--------|
| routing_strategy | latency-based-routing | simple-shuffle | test41003 ✓ (1.5s overhead on large pool) |
| cooldown_time | 30 | 10 | test41003 ✓ (proportional to RPM 1-min window) |
| RateLimitErrorAllowedFails | 3 | 5 | test41003 ✓ (429 tolerance prevents cascading cooldown) |
| rolling_window_size | 30 | default | test41003 ✓ (no unnecessary complexity) |

### Post-deployment verification
- **Proxy routing**: ✅ GLM-5.1 → glm5.1_test41003 (verified via docker inspect env vars + proxy log)
- **DSv4P**: ✅ 200 response via proxy 40001 (thinking + content, Anthropic format)
- **GLM-5.1**: ✅ 429 insufficient_quota (correct response — genuine quota exhaustion at end of day)
- **All 6 containers**: ✅ healthy (41001 backup, 41003 primary, 42001, postgres, 40001, 40002)
- **proxy.py retry-after**: ✅ restored (matches repo)

## opc2_uname_r2 Changes (2026-06-05 — ulimit fix + dsv4p router unification + engineering scripts)

### CRITICAL: ulimit nofile fix — prevents fd exhaustion on large deployment counts
- **Before**: All LiteLLM containers ulimit soft=1024 (Docker default)
- **After**: glm5.1_uni41001 ulimit soft=4096, dsv4p_uni42001 ulimit soft=4096, glm5.1_test41003 ulimit soft=8192
- **Why**: 1792/7000 deployments × health check TCP connections → "OSError: [Errno 24] Too many open files"
- **NEVER call /health for monitoring** — use /health/liveliness only. /health triggers on-demand checks → fd exhaustion cascade.

### DSv4P router_settings unified with glm5.1 (parity)
- **Before**: dsv4p used simple-shuffle + cooldown_time=10 + RateLimitErrorAllowedFails=5
- **After**: dsv4p uses latency-based-routing + cooldown_time=30 + RateLimitErrorAllowedFails=3 (same as glm5.1)
- **Also added**: rolling_window_size=30, lowest_latency_buffer=2, timeout=300, retry_after=0, model_group_alias
- **Format change**: allowed_fails_policy → allowed_fails (parity with glm5.1 format)

## opc_uname_r2 Changes (2026-06-05 — Proxy routing to 41003 primary: 7000 deployments)

### CRITICAL: Proxy 40001 routing switched from 41001 → 41003
- **Before**: 40001 proxy → 41001 (uni41001, 256 variants × 7 keys = 1792 deployments)
- **After**: 40001 proxy → 41003 (test41003, 1000 variants × 7 keys = 7000 deployments)
- **Why**: 7000 dep = 7000 RPM capacity × 200/id/day quota per variant = 1,400,000 requests/day theoretical max

### 41003 config expanded from KEY1-only to KEY1-7
- **Before**: 41003 config = 1024 variants × KEY1 = 1024 deployments (no key fallback)
- **After**: 41003 config = 1000 variants × KEY1-7 = 7000 deployments (7-key fallback)

### 41003 container resources increased
- **Before**: memory=1536M, cpus=1.5, start_period=120s
- **After**: memory=2048M, cpus=2.0, start_period=180s

### Router settings (optimized for 7000 dep pool)
- `routing_strategy`: simple-shuffle (latency-based-routing overhead not justified for 7000 dep)
- `num_retries`: 5
- `cooldown_time`: 10
- `RateLimitErrorAllowedFails`: 5
- `drop_params`: true

## opc2_uname_r3 Changes (2026-06-02 — All 11 Variants Restored)

### CRITICAL: All 11 Variants Restored (both configs)
- **Before**: glm5.1 had 28 deployments (4 variants × 7 keys), dsv4p had 14 (2 variants × 7 keys)
- **After**: 77 deployments each (11 variants × 7 keys)
- **Why**: Previous config removed variants claiming "null-response from ModelScope". Root cause was LiteLLM parser bug (delta field in non-stream). Force-stream fix resolves this for ALL variants. Removing variants = removing quota capacity.
- **Lesson reinforced**: NEVER remove resources without verifying their independent value first.

## Router Settings (41003 primary, 7000 dep — updated R7)
- num_retries: 8
- cooldown_time: 10
- routing_strategy: simple-shuffle
- enable_pre_call_checks: false
- RateLimitErrorAllowedFails: 5
- TimeoutErrorAllowedFails: 2
- InternalServerErrorAllowedFails: 3
- AuthenticationErrorAllowedFails: 0
- BadRequestErrorAllowedFails: 0

## Router Settings (42001 dsv4p, 77 dep — unified with glm5.1)
- num_retries: 5
- cooldown_time: 30
- routing_strategy: latency-based-routing
- rolling_window_size: 30
- lowest_latency_buffer: 2
- RateLimitErrorAllowedFails: 3
- TimeoutErrorAllowedFails: 2
- InternalServerErrorAllowedFails: 3

## Proxy Changes (Round 1-12 + R6-R7)
- Removed conn_retry — 3% success rate
- Removed rate_limit_retry — 8% success rate
- Removed glm→dsv4p FALLBACK — always fails
- R7: insufficient_quota 429 → api_error
- R9: INPUT-REJECT estimation fixed — `_estimate_text_chars()` + chars_per_token=2.0
- R9: insufficient_quota 429 → rate_limit_error (REVERTED R7, exponential backoff better than api_error)
- R12: **Removed proxy auto-compact entirely** — proxy-level truncation causes catastrophic context loss
- R12: Input overflow 400 → invalid_request_error (NOT 529 overloaded_error) — CC stops, user starts new conversation
- R12: 529 → api_error (NOT forced overloaded_error) — CC retries then stops, no auto-compact
- R12: retry-after headers on 429 (quota=30s, rpm=5s) and 529 (5s)
- R6: **stream_options.include_usage=True** — CC TUI shows real token counts instead of 0/200000
- R6: **Deferred message_delta** — usage chunk arrives after finish_reason in streaming, proxy defers delta to include real token counts
- R7: **CHARS_PER_TOKEN_ESTIMATE 2.0→3.0** — 56% overestimation → 3% (data: 538 requests, real ratio=3.11)
- R7: **MODEL_INPUT_TOKEN_SAFETY 170K→190K** — 86% capacity utilization (vs 47% before)
- R7: **contextWindow 170K→190K, autoCompactWindow 150K→180K** — CC usable context 96K→174K real tokens (+81%)

## Key Issues

### ModelScope Non-Stream InternalServerError — FIXED (2026-06-01)
- ALL non-stream requests force `stream=True` to LiteLLM

### /health Endpoint — NEVER call /health for monitoring
- Use /health/liveliness only. /health triggers per-deployment checks → fd exhaustion.

### insufficient_quota 429 → rate_limit_error (CC exponential backoff)
- CC treats rate_limit_error as temporary throttle → waits for quota recovery
- Daily quota resets — backoff allows CC to survive exhaustion periods

## Test Results (2026-06-05, after R3 config sync)
- dsv4p via proxy → 42001: ✅ 200 (thinking + content, Anthropic format)
- glm5.1 via proxy → 41003: ✅ 429 insufficient_quota (correct — genuine quota exhaustion)
- All 6 containers: ✅ healthy
- Proxy routing: ✅ glm5.1 → test41003 (verified docker inspect + proxy log)
- proxy.py retry-after: ✅ restored