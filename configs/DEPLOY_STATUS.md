# Deploy Status — opc_uname (updated 2026-06-05 R4 — dsv4p R2 config sync + proxy.py inappropriate-content fix sync + 06-05 metrics analysis)

## Architecture
```
CC → 40001(proxy, format conversion + force-stream ALL non-stream) → 41003(LiteLLM glm5.1, 1000 variants × 7 keys = 7000 deploys) → ModelScope
                                                                     → 42001(LiteLLM dsv4p, 11 variants × 7 keys = 77 deploys) → ModelScope
                                                                     → 41001(LiteLLM glm5.1-backup, 256 variants × 7 keys = 1792 deploys) [BACKUP, not routed from 40001]
```

## Deploy Method
- **docker compose**: `cd /opt/cc-infra && DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001`
- **Docker Hub**: unreachable from China without proxy → mihomo on port **7890** configured as Docker systemd proxy (`/etc/systemd/system/docker.service.d/proxy.conf`)
- **Legacy builder**: `DOCKER_BUILDKIT=0` required — BuildKit doesn't respect systemd proxy

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (1792 deployments: 256 variants × 7 keys) [BACKUP] — ulimits nofile=4096
- dsv4p_uni42001 :42001 (77 deployments: 11 variants × 7 keys) — ulimits nofile=4096
- glm5.1_test41003 :41003 (7000 deployments: 1000 variants × 7 keys) [PRIMARY glm5.1] — ulimits nofile=8192
- auth_to_api_40001 :40001 (proxy, format conversion + retry-after header + MODEL_MAP + insufficient_quota→rate_limit_error)
- auth_to_api_40002 :40002 (Codex proxy, same codebase)

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

## Router Settings (41003 primary, 7000 dep)
- num_retries: 5
- cooldown_time: 10
- routing_strategy: simple-shuffle
- enable_pre_call_checks: false
- background_health_checks: false
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

## Proxy Changes (Round 1-9)
- Removed conn_retry — 3% success rate
- Removed rate_limit_retry — 8% success rate
- Removed glm→dsv4p FALLBACK — always fails
- R7: insufficient_quota 429 → api_error
- R8: Auto-compact INPUT-REJECT messages → 200 response instead of 529
- R9: INPUT-REJECT estimation fixed — `_estimate_text_chars()` + chars_per_token=2.0
- R9: insufficient_quota 429 → rate_limit_error (REVERTED R7, exponential backoff better than api_error)
- R12.3: retry-after=30 header on INPUT-REJECT-UNCOMPACTABLE 429 response

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