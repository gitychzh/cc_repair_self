# Deploy Status — opc_uname (R18, 2026-06-11)

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
| dsv4p_uni42001 | :42001 | dsv4p | 77 deploys, ulimits nofile=4096 |
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

## Metrics Summary (06-11, latest full day)

| Metric | 40001 (opc_uname) | 40002 (opc2_uname) |
|--------|-------------------|---------------------|
| Total requests | 596 | 32 |
| Success rate | 100% | 100% |
| Errors | 0 | 0 |
| Avg latency | 20.8s | 9.2s |
| P50 latency | 19.0s | 8.6s |
| P90 latency | 33.3s | — |
| P99 latency | 65.0s | — |
| Actual chars/token | 2.87 avg (CPT=3.0) | — |
| Max est_tokens | 130K (83.9% of 155K) | — |
| MS quota remaining | 196-199 avg=199 | — |

**06-11 analysis**: 596 reqs, 100% success (ZERO errors), avg latency 20.8s, max est_tokens 130K (never hit autoCompactWindow 155K), quota 196-199. CC overestimation ratio est/actual=0.95 (slight underestimate on Jun 11 vs 1.24 overestimate on Jun 10 — content composition variance). 'length' finish_reason: 12 requests, all startup connectivity checks (input_tokens≤7, harmless). Proxy overhead median=507ms, avg=4.7s (correlates with output token count — expected streaming behavior).

## Historical Trend

| Day | Total | Success | Avg Latency | Notes |
|-----|-------|---------|-------------|-------|
| 06-02 | 243 | 80.2% | ~14s | Pre-R12 (proxy auto-compact) |
| 06-03 | 1214 | 84.2% | ~14s | 47 InputExceedsProxyReject |
| 06-05 | 1558 | 80.7% | ~14s | Pre-R12 |
| 06-09 | 220 | 96.8% | 13.9s | Post-R12, startup errors |
| 06-10 | 707 | 99.6% | 19.3s | Post-R7 |
| 06-10 | 1887 | 99.8% | 20.7s | Post-R15/R16, best ever |
| 06-11 | 596 | 100% | 20.8s | Zero errors, CPT=2.87 actual, est≤130K |

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality
- **CC tokenizer estimation variance**: Jun 10 est/actual=1.24 (overestimate), Jun 11 est/actual=0.95 (slight underestimate) — content composition variance makes prediction unreliable. autoCompactWindow=155K balances both scenarios
- Write critical info to CLAUDE.md/memory — these survive compaction

### CHARS_PER_TOKEN_ESTIMATE — resolved ✅
- Both containers now running CPT=3.0 (Jun 11 metrics confirm ratio=3.0005)
- Previous discrepancy (docker-compose=3.0 vs running=2.0) was from container restart without recreate
- Resolved by force-recreate during R15/16 deployment on Jun 10

### CC v2.1.170 startup connectivity check
- Uses **shell env vars** (ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, HTTPS_PROXY), NOT settings.json
- Three-layer persistence: .bashrc (before non-interactive return) + .profile (login shells) + restart_claude.sh (`bash --login -c`)
- Without shell env vars → CC connects api.anthropic.com → 401 → refuses to start

### Proxy NEVER truncates/compacts (R12 principle)
- Proxy-level truncation causes catastrophic context loss ("completely forgets everything")
- CC built-in auto-compact is sole mechanism — same outcome but CC's own decision
- Input overflow → invalid_request_error (CC stops, user starts new conversation)

### ModelScope limits
- Input token limit: 202,745 (confirmed by ModelScope error)
- Quota: 200 requests/id/day per variant × 1000 variants = 200K/day theoretical max
- Daily quota resets; 429 insufficient_quota is genuine exhaustion, not config fixable

### /health endpoint — NEVER use /health for monitoring
- Use /health/liveliness only. /health triggers per-deployment checks → fd exhaustion.

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
| R18 | Tier-based routing (cc-switch inspired) + THINKING_SUPPORT dict + LITELLM_MODELS_URL bug fix (40002 missing both, 40001 missing dsv4p) + _anthropic_models_list expansion (2→29 aliases) + haiku→dsv4p routing fix + gateway package sync | Pending remote validation |
| R14 | Shell env vars fix (.bashrc+.profile+restart_claude.sh) | CC startup stable |

## 11 Immutable Variant Model IDs

**GLM-5.1 (41003/41001):** `zhipuai/glm-5.1`, `ZHipuAI/GlM-5.1`, `ZhIpuAI/GLm-5.1`, `ZhiPuAI/gLM-5.1`, `ZhipUAI/GlM-5.1`, `ZhipuAi/GLM-5.1`, `ZhipuaI/GLm-5.1`, `zhipuAI/gLM-5.1`, `ZHIPUAI/GLM-5.1`, `zhipuai/GLM-5.1`, `ZhiPUAI/glm-5.1`

**DSv4P (42001):** `deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`, `deepseek-ai/DeEpSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**