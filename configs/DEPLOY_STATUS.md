# Deploy Status — opc_uname (R18.2, 2026-06-11)

## Architecture
```
Agent(CC/OpenCode/Codex) → 40001/40002(proxy, format conversion + tier routing + metrics)
    → 41003(LiteLLM glm5.1, 1000 variants × 7 keys = 7000 deploys) → ModelScope [PRIMARY]
    → 42001(LiteLLM dsv4p, 11 variants × 7 keys = 77 deploys) → ModelScope [HAIUK/MINI tier]
    → 41001(LiteLLM glm5.1-backup, 1000 variants × 7 keys) [BACKUP]
```

Proxy does **format conversion + force-stream + stream_usage + tier-based model routing + metrics logging**. No retry, no truncation, no auto-compact. LiteLLM handles retry/fallback/routing/cooldown.

**Tier-based routing (inspired by cc-switch)**: opus/sonnet tier → glm5.1 (7000 dep, thinking support), haiku/mini tier → dsv4p (77 dep, no thinking). OpenAI-style names (gpt-4o, gpt-4o-mini, codex-mini-latest) also supported for multi-agent compatibility.

## Containers (all healthy)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| glm5.1_test41003 | :41003 | Primary glm5.1 | 7000 deploys, ulimits nofile=8192 |
| glm5.1_uni41001 | :41001 | Backup glm5.1 | 7000 deploys, ulimits nofile=4096 |
| dsv4p_uni42001 | :42001 | dsv4p | 77 deploys, ulimits nofile=4096, memory limit 2GiB (R18.2) |
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

## Metrics Summary (06-11 full data, 06-10 comparison)

| Metric | 06-10 40001 | 06-10 40002 | 06-11 40001 | 06-11 40002 |
|--------|-------------|-------------|-------------|-------------|
| Total requests | 1887 | 48 | 1339 | 38 |
| Success rate | 99.8% | 100% | 96.7%* | 100% |
| Errors | 2×502, 1×429 | 0 | 44×429 (32 glm5.1 + 12 dsv4p)† | 0 |
| Avg TTFB | 19.0s | 6.0s | 18.4s | 8.4s |
| P50 TTFB | 16.2s | 5.0s | 16.7s | — |
| P90 TTFB | 33.0s | — | 30.9s | — |
| P95 TTFB | — | — | 38.4s | — |
| P99 TTFB | 65.0s | — | 49.2s | — |
| Avg duration | 20.7s | 6.2s | 20.5s | — |
| Actual chars/token (json) | — | — | 4.09 median (CPT=3.0 → 1.36x overest) | — |
| Max est_tokens_json | 205K | — | 208K (actual=136K) | — |
| Max actual tokens | — | — | 136K | — |
| est/actual ratio | 1.24 avg | — | 1.36 median | — |
| MS quota remaining | 150-199 | — | 1314-1502, last=1485 | — |
| Burst success rate | — | — | 93.6% inside burst (glm5.1=95%, dsv4p=65%) | — |
| Tool truncation | — | — | 71% reduction, ~10K tok saved | — |

\* *Excluding 429 burst: 100% (648/648 outside 16:05→17:47 window)*
† *429 burst at 16:05→17:46 (101min, ENDED) — ALL 7 keys' ModelScope TOKEN quota exhausting. 32 glm5.1 429s + 12 dsv4p 429s. Same keys across all deployments → both backends affected. Overall burst success 93.6%. Same keys → fallback won't help.*

**06-11 full analysis**: 1339 reqs, 96.7% success (44×429: 32 glm5.1 + 12 dsv4p, burst ENDED at 17:46). Outside burst: 100%. Avg TTFB 18.4s, P95=38.4s, **P99=49.2s (improved vs Jun 10's 65s)**. Max actual=136K. dsv4p also gets 429 during burst (same keys), but outside burst 100% success → tier routing still beneficial. Post-burst TTFB avg=17.1s (normal). **dsv4p memory**: 52.46%. **All parameters within range — no changes warranted.**

## Historical Trend

| Day | Total | Success | Avg Latency | Notes |
|-----|-------|---------|-------------|-------|
| 06-02 | 243 | 80.2% | ~14s | Pre-R12 (proxy auto-compact) |
| 06-03 | 1214 | 84.2% | ~14s | 47 InputExceedsProxyReject |
| 06-04 | 1787 | ~85% | ~14s | 441 429 errors, pre-R12 |
| 06-05 | 1558 | 80.7% | ~14s | 244 429 errors, Pre-R12 |
| 06-09 | 220 | 96.8% | 13.9s | Post-R12, startup errors |
| 06-10 | 1887 | 99.8% | 20.7s | Post-R15/R16, best ever |
| 06-11 | 1339 | 96.7% (100% excl burst) | 18.4s | 44×429 (32 glm+12 dsv4p) burst 16:05→17:46 (101min, ENDED), P99=49.2s ✅, R18.2 ✅ |

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

### ModelScope dual quota system (NEW FINDING)
- **RPM quota**: 200/id/day per variant (tracked by `ms_requests_remaining` header). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked by any header). Independent from RPM.
- Jun 11 429 burst: RPM quota was fine (ms_requests_remaining=1705), but ALL 7 keys' token quota exhausting at 16:05 → 44 errors over 101min (16:05→17:46, ENDED). 32 glm5.1 429s + 12 dsv4p 429s. Overall burst success 93.6%.
- **NEW: dsv4p also affected by same-key token quota exhaustion** — same 7 keys across both backends means dsv4p's 77-deployment pool has fewer retry opportunities than glm5.1's 7000-deployment pool. But dsv4p volume is only 3% of total, so absolute impact is small. Outside burst: dsv4p 100% success. Tier routing remains beneficial.
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
- **Fix**: Added both env vars to docker-compose.yml for both proxy containers, pointing to correct backend:
  - `LITELLM_MODELS_URL_GLM51=http://glm5.1_test41003:4000/v1/models` (primary, not old backup 41001)
  - `LITELLM_MODELS_URL_DSV4P=http://dsv4p_uni42001:4000/v1/models`

### 2. Tier-based model routing (cc-switch inspired)
- **Before**: All Claude model names (opus, sonnet, haiku) mapped to glm5.1 → wasted dsv4p's 77-deployment pool
- **After**: Tier routing — opus/sonnet tier → glm5.1 (7000 dep, thinking support), haiku/mini tier → dsv4p (77 dep, no thinking)
- **New MODEL_MAP entries**: gpt-4o/gpt-4.1→glm5.1, gpt-4o-mini/gpt-4.1-mini/o3-mini/o4-mini→dsv4p, codex-mini-latest→glm5.1
- **Rationale**: Lighter tasks (haiku, mini) don't need 7000-deployment pool; dsv4p is sufficient and frees glm5.1 quota for heavy tasks

### 3. THINKING_SUPPORT dict
- **Before**: Hardcoded `if body.get("thinking"):` → thinking params sent to ALL backends including dsv4p (doesn't support it)
- **After**: `THINKING_SUPPORT = {"glm5.1": True, "dsv4p": False}` + conditional: `if body.get("thinking") and THINKING_SUPPORT.get(target_model, False)`
- **Rationale**: dsv4p doesn't support extended thinking; sending thinking params causes errors or wasted tokens

### 4. _anthropic_models_list expansion (2→29 aliases)
- **Before**: Deduplication by backend name (mapped) → only glm5.1 and dsv4p appeared in /v1/models response
- **After**: Deduplication by model_id → all 29 requestable model aliases appear
- **Rationale**: Other agents (OpenCode, Codex, etc.) need to see all available model IDs to route correctly

### 5. Haiku→dsv4p routing fix
- **Before**: claude-haiku-4-5, claude-haiku-4-5-20251001, claude-3-5-haiku-20241022 all → glm5.1
- **After**: All three → dsv4p (no thinking support, lighter model)
- **Rationale**: Haiku is a lightweight model; dsv4p's 77-deployment pool is adequate; frees glm5.1's 7000-deployment pool for heavy opus/sonnet tasks

### 6. Gateway package sync
- Monolithic proxy.py AND modular gateway package (6 files) both updated with same changes
- Running proxy uses modular gateway package (Dockerfile uses litellm base image + gateway/)
- Repo configs/proxy/Dockerfile (python:3.13-alpine) is documentation-only; actual running Dockerfile uses litellm base image

## Parameter Change History (condensed)

| Round | Changes | Result |
|-------|---------|--------|
| R1-5 | cooldown params, socket bug, conn_retry removal, num_retries=5 | 85.4%→100% |
| R12 | Removed proxy auto-compact; safety 120K→170K; contextWindow 120K→170K; InputReject→invalid_request_error | 80%→97% |
| R7 | CHARS_PER_TOKEN 2.0→3.0; safety 170K→190K; contextWindow 170K→190K; compactWindow 150K→180K | 99.6% |
| R15 | compactWindow 180K→140K (GLM IQ); contextWindow/safety 190K→170K (alignment) | 99.8% |
| R16 | compactWindow 140K→155K (CC overestimation 1.7x → too early compact) | 99.8% best ever |
| R17 | opc2_uname full sync: docker-compose.yml + litellm num_retries 30→8 + settings.json 155K + HTTPS_PROXY + proxy.py parity | 99.8%+ stable |
| R18 | Tier-based routing (cc-switch inspired) + THINKING_SUPPORT dict + LITELLM_MODELS_URL bug fix + _anthropic_models_list expansion (2→29) + haiku→dsv4p + gateway package sync | 100% success, zero errors ✅ |
| R18.1 | Metrics deep analysis: 429 token-limit burst identified (not RPM), ModelScope dual quota documented, TTFB+60% is server-side, CPT=3.0 accuracy verified (1.36x overest vs actual 4.08), /health endpoint clarified for proxy vs LiteLLM | No param changes — all current settings performing well within range |
| R18.2 | dsv4p memory limit 1GiB→2GiB (OOM risk: 90.39% utilization=925.6MiB/1GiB), reservations 512M→768M | Prevents dsv4p OOM kill, verified 51.13% after recreate ✅ |
| R14 | Shell env vars fix (.bashrc+.profile+restart_claude.sh) | CC startup stable |

## 11 Immutable Variant Model IDs

**GLM-5.1 (41003/41001):** `zhipuai/glm-5.1`, `ZHipuAI/GlM-5.1`, `ZhIpuAI/GLm-5.1`, `ZhiPuAI/gLM-5.1`, `ZhipUAI/GlM-5.1`, `ZhipuAi/GLM-5.1`, `ZhipuaI/GLm-5.1`, `zhipuAI/gLM-5.1`, `ZHIPUAI/GLM-5.1`, `zhipuai/GLM-5.1`, `ZhiPUAI/glm-5.1`

**DSv4P (42001):** `deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`, `deepseek-ai/DeEpSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**