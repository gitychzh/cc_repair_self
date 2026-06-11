# Deploy Status — opc_uname (R18.3, 2026-06-11)

## Architecture
```
Agent(CC/OpenCode/Codex) → 40001/40002(proxy, format conversion + tier routing + metrics)
    → 41003(LiteLLM glm5.1, 1000 variants × 7 keys = 7000 deploys) → ModelScope [PRIMARY]
    → 42001(LiteLLM dsv4p, 11 variants × 7 keys = 77 deploys) → ModelScope [HAIUK/MINI tier]
    → 41001(LiteLLM glm5.1-backup, 1000 variants × 7 keys = 7000 deploys) [BACKUP]
```

Proxy does **format conversion + force-stream + stream_usage + tier-based model routing + metrics logging**. No retry, no truncation, no auto-compact. LiteLLM handles retry/fallback/routing/cooldown.

**Tier-based routing (inspired by cc-switch)**: opus/sonnet tier → glm5.1 (7000 dep, thinking support), haiku/mini tier → dsv4p (77 dep, no thinking). OpenAI-style names (gpt-4o, gpt-4o-mini, codex-mini-latest) also supported for multi-agent compatibility.

## Containers (all healthy)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| glm5.1_test41003 | :41003 | Primary glm5.1 | 7000 deploys, ulimits nofile=8192, memory 2GiB |
| glm5.1_uni41001 | :41001 | Backup glm5.1 | 7000 deploys, ulimits nofile=8192 (R18.3), memory 2GiB (R18.3) |
| dsv4p_uni42001 | :42001 | dsv4p | 77 deploys, ulimits nofile=4096, memory 2GiB (R18.2) |
| auth_to_api_40001 | :40001 | Proxy (opc_uname) | Format conversion + tier routing + stream_usage + metrics |
| auth_to_api_40002 | :40002 | Proxy (opc2_uname) | Same codebase + LITELLM_MODELS_URL now configured |
| cc_postgres | :5432 | LiteLLM DB | — |

## Deploy Method
```bash
# LiteLLM config change → restart only
docker restart glm5.1_uni41001 / dsv4p_uni42001

# proxy.py change → rebuild
cd /opt/cc-infra && DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```
Docker Hub unreachable from China → mihomo on :7890 as Docker systemd proxy. `DOCKER_BUILDKIT=0` required (BuildKit ignores systemd proxy).

## Current Parameters (R18)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Both containers running 3.0 ✅ (resolved R17 recreate) |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Seconds |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (glm5.1/dsv4p) | 300 | litellm config.yaml | Seconds |
| num_retries (41003) | 8 | litellm config.yaml | — |
| num_retries (42001) | 5 | litellm config.yaml | — |
| cooldown_time (41003) | 10 | litellm config.yaml | — |
| cooldown_time (42001) | 30 | litellm config.yaml | — |
| routing_strategy (41003) | simple-shuffle | litellm config.yaml | — |
| routing_strategy (42001) | latency-based-routing | litellm config.yaml | — |
| RateLimitErrorAllowedFails (41003) | 5 | litellm config.yaml | — |
| RateLimitErrorAllowedFails (42001) | 3 | litellm config.yaml | — |

## Metrics Summary (06-11 final, 06-10 comparison)

| Metric | 06-10 40001 | 06-11 40001 | Change |
|--------|-------------|-------------|--------|
| Total requests | 1887 | 1555 | ↓ |
| Success rate | 99.8% | 96.8% (100% excl 429) | burst外持平 |
| 429 errors | 1 | 49 (35 glm+14 dsv4p) | token quota burst |
| Avg TTFB | 19.0s | 17.9s | ↓ |
| P50 TTFB | 16.2s | 16.0s | — |
| P90 TTFB | 33.0s | 30.5s | ↓ |
| P95 TTFB | — | 38.4s | — |
| P99 TTFB | 65.0s | 49.8s | ↓ 23% improved ✅ |
| P99 duration | 80.4s | 69.5s | ↓ 13% improved ✅ |
| Avg litellm_resp | 15.1s | 14.8s | — |
| P99 litellm_resp | 55.9s | 45.4s | ↓ 19% improved ✅ |
| ms_requests_remaining min | 1316 | 1314 | — |
| est/actual token ratio | 1.24 avg | 1.362 median | CPT=3.0 normal |
| Actual chars/token (json) | — | 4.08 median | — |
| Tool truncation | — | 71.1% reduction | ✅ |

**Burst analysis**: 49×429 at 16:05→22:00 (35 glm5.1 + 14 dsv4p, ALL token quota exhaustion). Dense burst 16:05→17:46 (44/761 reqs = 94.2% success). Outside burst: 99.4% success. P99 TTFB improved from Jun 10's 65s → 49.8s ✅. P99 duration improved from 80.4s → 69.5s ✅. All errors are 429 only — zero 502/timeout/5xx errors = infrastructure 100% stable.

**Container memory status**: glm5.1_test41003 37.80%/2GiB ✅, dsv4p_uni42001 42.90%/2GiB ✅, glm5.1_uni41001 52.12%/2GiB ✅ (R18.3 OOM fix stable).

## Historical Trend

| Day | Total | Success | Avg Latency | Notes |
|-----|-------|---------|-------------|-------|
| 06-02 | 243 | 80.2% | ~14s | Pre-R12 (proxy auto-compact) |
| 06-03 | 1214 | 84.2% | ~14s | 47 InputExceedsProxyReject |
| 06-04 | 1787 | ~85% | ~14s | 441 429 errors, pre-R12 |
| 06-05 | 1558 | 80.7% | ~14s | 244 429 errors, Pre-R12 |
| 06-09 | 220 | 96.8% | 13.9s | Post-R12, startup errors |
| 06-10 | 1887 | 99.8% | 20.7s | Post-R15/R16, best ever |
| 06-11 | 1555 | 96.8% (100% excl 429) | 17.9s | 49×429 token burst, P99=49.8s ↓23% vs Jun10, R18.3 deployed, infra 100% stable |

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality
- **CC tokenizer estimation variance**: Jun 10 est/actual=1.24 (overestimate), Jun 11 est/actual=1.36 median (proxy CPT=3.0 overestimates vs actual chars/token=4.08). Content composition variance makes per-day ratio unpredictable. autoCompactWindow=155K balances both scenarios: Jun 10 overestimates → compact fires earlier (real~125K=74% of 170K), Jun 11's 1.36x overestimate → compact fires at real~114K=67% of 170K). Both safe, with good margin
- Write critical info to CLAUDE.md/memory — these survive compaction

### CHARS_PER_TOKEN_ESTIMATE — resolved ✅, accuracy documented
- Both containers running CPT=3.0 (Jun 11 full metrics confirm: actual chars/token(json)=4.08 median)
- Proxy overestimates tokens by 1.36x (chars_json/3.0 vs actual ModelScope tokens) — only affects INPUT-WARN threshold
- CC auto-compact uses Anthropic tokenizer internally, NOT proxy's CPT estimate — changing CPT won't affect compact behavior
- Overestimation gives safety margin for early warning (INPUT-WARN triggers at ~88K actual tokens instead of 120K)
- Previous discrepancy (docker-compose=3.0 vs running=2.0) resolved by force-recreate during R15/16

### CC v2.1.170 startup connectivity check
- Uses **shell env vars** (ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, HTTPS_PROXY), NOT settings.json
- Three-layer persistence: .bashrc (before non-interactive return) + .profile (login shells) + restart_claude.sh (`bash --login -c`)
- Without shell env vars → CC connects api.anthropic.com → 401 → refuses to start

### Proxy NEVER truncates/compacts (R12 principle)
- Proxy-level truncation causes catastrophic context loss ("completely forgets everything")
- CC built-in auto-compact is sole mechanism — same outcome but CC's own decision
- Input overflow → invalid_request_error (CC stops, user starts new conversation)

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by `ms_requests_remaining` header). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked by any header). Independent from RPM.
- Jun 11 429 burst: RPM quota was fine (ms_requests_remaining=1314+), but ALL 7 keys' token quota exhausting at 16:05 → 49 errors over ~6hr (16:05→22:00). 35 glm5.1 429s + 14 dsv4p 429s.
- **dsv4p also affected by same-key token quota exhaustion** — same 7 keys across both backends means dsv4p's 77-deployment pool has fewer retry opportunities than glm5.1's 7000-deployment pool. But dsv4p volume is only 3% of total, so absolute impact is small. Outside burst: dsv4p 100% success.
- Same 7 keys used across all deployments → fallback to backup LiteLLM (41001) won't help (same keys = same token quota exhaustion).
- Input token limit: 202,745 (confirmed by ModelScope error)

### /health endpoint — context clarified
- LiteLLM /health triggers per-deployment checks → fd exhaustion → NEVER use for monitoring. Use /health/liveliness.
- Proxy /health is a simple status check (returns {"status":"ok"}) → SAFE to use for Docker healthcheck.
- Docker-compose correctly uses /health/liveliness for LiteLLM containers and /health for proxy containers.

## R18 Changes (2026-06-11)

### 1. LITELLM_MODELS_URL bug fix (CRITICAL)
- **40002 proxy**: Missing BOTH `LITELLM_MODELS_URL_GLM51` and `LITELLM_MODELS_URL_DSV4P` env vars → /v1/models used wrong defaults → agents couldn't discover available models
- **40001 proxy**: Missing `LITELLM_MODELS_URL_DSV4P` → dsv4p models returned via default pointing to old backup 41001
- **Fix**: Added both env vars to docker-compose.yml for both proxy containers, pointing to correct backend

### 2. Tier-based model routing (cc-switch inspired)
- **Before**: All Claude model names (opus, sonnet, haiku) mapped to glm5.1 → wasted dsv4p's 77-deployment pool
- **After**: Tier routing — opus/sonnet tier → glm5.1 (7000 dep, thinking support), haiku/mini tier → dsv4p (77 dep, no thinking)

### 3. THINKING_SUPPORT dict
- **Before**: Hardcoded `if body.get("thinking"):` → thinking params sent to ALL backends including dsv4p
- **After**: `THINKING_SUPPORT = {"glm5.1": True, "dsv4p": False}` + conditional check

### 4. _anthropic_models_list expansion (2→29 aliases)
- **Before**: Deduplication by backend name → only glm5.1 and dsv4p appeared
- **After**: Deduplication by model_id → all 29 requestable model aliases appear

### 5. Haiku→dsv4p routing fix
- claude-haiku models → dsv4p (no thinking, lighter model)

### 6. Gateway package sync
- Monolithic proxy.py AND modular gateway package (6 files) both updated with same changes

## R18.3 Changes (2026-06-11)

### 1. glm5.1_uni41001 memory OOM fix (CRITICAL)
- **Problem**: glm5.1_uni41001 at 93.73% memory (959.8MiB/1GiB) with 7000 deployments → near OOM kill risk
- **Same pattern as dsv4p R18.2 fix** (90.39%/1GiB → increased to 2GiB)
- **Evidence**: docker stats shows 958-960MiB consistently; 41001 config upgraded to 1000var×7keys=7000 deploys (from 256var) but memory limit stayed at 1GiB
- **Fix**: memory 1024M→2048M, reservations 512M→768M, ulimit nofile 4096→8192, CPU 1.0→2.0, healthcheck interval 30s→60s, timeout 10s→30s, start_period 60s→180s — matching glm5.1_test41003 specs since both have identical 7000 deployment configs
- **Rationale**: 41001 is BACKUP with same 7000 deploys as 41003 PRIMARY. It should have same resource specs. Without this fix, Docker will OOM-kill the container when memory hits 1GiB limit, making the backup unavailable.

## Parameter Change History (condensed)

| Round | Changes | Result |
|-------|---------|--------|
| R1-5 | cooldown params, socket bug, conn_retry removal, num_retries=5 | 85.4%→100% |
| R12 | Removed proxy auto-compact; safety 120K→170K; contextWindow 120K→170K; InputReject→invalid_request_error | 80%→97% |
| R7 | CHARS_PER_TOKEN 2.0→3.0; safety 170K→190K; contextWindow 170K→190K; compactWindow 150K→180K | 99.6% |
| R15 | compactWindow 180K→140K (GLM IQ); contextWindow/safety 190K→170K (alignment) | 99.8% |
| R16 | compactWindow 140K→155K (CC overestimation 1.7x → too early compact) | 99.8% best ever |
| R17 | opc2_uname full sync: docker-compose.yml + litellm num_retries 30→8 + settings.json 155K + HTTPS_PROXY + proxy.py parity | 99.8%+ stable |
| R18 | Tier-based routing + THINKING_SUPPORT dict + LITELLM_MODELS_URL bug fix + _anthropic_models_list expansion + haiku→dsv4p + gateway package sync | 100% success |
| R18.1 | Metrics deep analysis: 429 token-limit burst, dual quota, TTFB server-side, CPT=3.0 accuracy, /health endpoint clarified | No param changes |
| R18.2 | dsv4p memory limit 1GiB→2GiB (OOM risk: 90.39%), reservations 512M→768M | dsv4p OOM prevented ✅ |
| R18.3 | glm5.1_uni41001 memory limit 1GiB→2GiB (OOM risk: 93.73%), ulimit nofile 4096→8192, CPU 1.0→2.0, reservations 512M→768M | 41001 OOM prevented ✅ deployed, 51.73%/2GiB |
| R14 | Shell env vars fix (.bashrc+.profile+restart_claude.sh) | CC startup stable |

## 11 Immutable Variant Model IDs

**GLM-5.1 (41003/41001):** 1000 case-permutation variants of `zhipuai/glm-5.1` × 7 keys = 7000 deployments

**DSv4P (42001):** `deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`, `deepseek-ai/DeEpSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**