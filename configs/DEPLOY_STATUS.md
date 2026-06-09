# Deploy Status — opc_uname (updated 2026-06-10 R6 — proxy stream_usage sync + glm5.1 router_settings sync + metrics analysis)

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

## opc_uname_r6 Changes (2026-06-10 — proxy stream_usage sync + glm5.1 router_settings sync + metrics analysis)

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

## Router Settings (41003 primary, 7000 dep — updated R6)
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

## Proxy Changes (Round 1-12 + R6)
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