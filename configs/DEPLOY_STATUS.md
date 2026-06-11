# Deploy Status — opc_uname + opc2_uname (R19, 2026-06-12)

## Architecture (same on both machines)
```
Agent(CC/OpenCode/Codex) → 40001/40002(proxy, format conversion + tier routing + metrics)
    → 41003(LiteLLM glm5.1, 1000 variants × 7 keys = 7000 deploys) → ModelScope [PRIMARY]
    → 42001(LiteLLM dsv4p, 11 variants × 7 keys = 77 deploys) → ModelScope [HAiku/Mini tier]
    → 41001(LiteLLM glm5.1-backup, 1000 variants × 7 keys = 7000 deploys) [BACKUP]
```

Proxy does **format conversion + force-stream + stream_usage + tier-based model routing + metrics logging**. No retry, no truncation, no auto-compact. LiteLLM handles retry/fallback/routing/cooldown.

**Tier-based routing (inspired by cc-switch)**: opus/sonnet tier → glm5.1 (7000 dep, thinking support), haiku/mini tier → dsv4p (77 dep, no thinking). OpenAI-style names (gpt-4o, gpt-4o-mini, codex-mini-latest) also supported for multi-agent compatibility.

## Containers (all healthy on both machines)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| glm5.1_test41003 | :41003 | Primary glm5.1 | 7000 deploys, ulimits nofile=8192, memory 2GiB |
| glm5.1_uni41001 | :41001 | Backup glm5.1 | 7000 deploys, ulimits nofile=8192 (R18.3), memory 2GiB (R18.3) |
| dsv4p_uni42001 | :42001 | dsv4p | 77 deploys, ulimits nofile=4096, memory 2GiB (R18.2) |
| auth_to_api_40001 | :40001 | Proxy (opc_uname/opc2_uname) | R18 proxy.py ✅ (R19 rebuild on opc2_uname) |
| auth_to_api_40002 | :40002 | Proxy (opc2_uname) | R18 proxy.py ✅ (R19 rebuild on opc2_uname) |
| cc_postgres | :5432 | LiteLLM DB | — |

## Deploy Method
```bash
# LiteLLM config change → restart only
docker restart glm5.1_uni41001 / dsv4p_uni42001

# proxy.py change → rebuild (use local Dockerfile with cached litellm/litellm:v1.87.0 image)
cd /opt/cc-infra && DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```
Docker Hub unreachable from China → mihomo on :7890 as Docker systemd proxy. `DOCKER_BUILDKIT=0` required (BuildKit ignores systemd proxy). **Note**: ghcr.io also unreachable → Dockerfile now uses locally cached `litellm/litellm:v1.87.0` image instead of `ghcr.io/berriai/litellm:v1.83.14-stable.patch.1`.

## Current Parameters (R19)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Both containers running 3.0 ✅ |
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

## Metrics Summary (06-11 opc_uname 40001, 06-11 opc2_uname 40001)

### opc_uname 40001 (from DEPLOY_STATUS R18.3)
| Metric | 06-10 | 06-11 | Change |
|--------|-------|-------|--------|
| Total requests | 1887 | 1555 | ↓ |
| Success rate | 99.8% | 96.8% (100% excl 429) | burst外持平 |
| 429 errors | 1 | 49 (token quota burst) | transient |
| P99 TTFB | 65.0s | 49.8s | ↓23% ✅ |
| P99 duration | 80.4s | 69.5s | ↓13% ✅ |

### opc2_uname 40001 (R19 analysis)
| Metric | 06-09 | 06-10 | 06-11 | Trend |
|--------|-------|-------|-------|-------|
| Total requests | 638 | 771 | 248 | — |
| Stable success rate | 100% | 99.9% | 99.6% | ✅ stable |
| 429 errors | 0 | 0 | 1 | token quota burst (21:06) |
| Startup 502s | 0 | 0 | 9 | container restart, transient |
| TTFB p99 | 42.5s | 56.8s | 39.8s | improved ✅ |
| Duration p99 | 52.2s | 82.9s | 58.2s | improved ✅ |
| ms_requests_remaining min | 1465 | 1323 | 1217 | healthy (>1200) |
| chars/token median | N/A | N/A | 4.11 | CPT=3.0 overestimates 1.36x |

**Burst analysis (opc2_uname 06-11)**: 9×502 startup errors at 15:38→15:40 (container restart), 1×429 token quota burst at 21:06. Excluding startup: 99.6% success rate. TTFB p99=39.8s improved vs Jun 10's 56.8s ✅. Infrastructure 100% stable outside startup and token quota burst.

**Container memory status (opc2_uname R19)**: glm5.1_test41003 35.55%/2GiB ✅, dsv4p_uni42001 30.49%/2GiB ✅, glm5.1_uni41001 33.28%/2GiB ✅ (R18.3 OOM fix stable, previously was 74.76%/1GiB).

## Historical Trend

| Day | Total | Success | Avg Latency | Notes |
|-----|-------|---------|-------------|-------|
| 06-02 | 243 | 80.2% | ~14s | Pre-R12 (proxy auto-compact) |
| 06-03 | 1214 | 84.2% | ~14s | 47 InputExceedsProxyReject |
| 06-04 | 1787 | ~85% | ~14s | 441 429 errors, pre-R12 |
| 06-05 | 1558 | 80.7% | ~14s | 244 429 errors, Pre-R12 |
| 06-09 | 220 | 96.8% | 13.9s | Post-R12, startup errors |
| 06-10 | 1887 | 99.8% | 20.7s | Post-R15/R16, best ever |
| 06-11 | 1555 | 96.8% (100% excl 429) | 17.9s | 49×429 token burst, P99=49.8s ↓23%, infra stable |
| 06-12 | 248+ | 99.6% (excl startup) | 15.2s | R19 deployed, proxy rebuilt, all 6 containers healthy |

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality
- **CC tokenizer estimation variance**: Jun 11 est/actual=1.36 median (proxy CPT=3.0 overestimates vs actual chars/token=4.08). autoCompactWindow=155K provides safe margin.
- Write critical info to CLAUDE.md/memory — these survive compaction

### CHARS_PER_TOKEN_ESTIMATE — resolved ✅
- CPT=3.0 overestimates by 1.36x (chars_json/3.0 vs actual) — only affects INPUT-WARN threshold
- CC auto-compact uses Anthropic tokenizer internally, NOT proxy's CPT — changing CPT won't affect compact behavior

### CC v2.1.170 startup connectivity check
- Uses **shell env vars** (ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, HTTPS_PROXY), NOT settings.json
- Three-layer persistence: .bashrc + .profile + restart_claude.sh (`bash --login -c`)

### Proxy NEVER truncates/compacts (R12 principle)
- Proxy-level truncation causes catastrophic context loss
- CC built-in auto-compact is sole mechanism

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by ms_requests_remaining). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked). Independent from RPM.
- Jun 11 429 burst: RPM quota fine (ms_requests_remaining=1314+), but 7 keys' token quota exhausted → 429 errors
- Same 7 keys across all deployments → fallback to backup won't help (same token quota)
- Token quota burst is transient (15-20 min recovery), NOT configurable

### /health endpoint — NEVER use on LiteLLM
- LiteLLM /health → per-deployment checks → fd exhaustion. Use /health/liveliness.
- Proxy /health → simple status check → SAFE for Docker healthcheck.

## R19 Changes (2026-06-12, opc2_uname)

### 1. Proxy rebuild with R18 proxy.py (CRITICAL)
- **Problem**: Both proxy containers (40001/40002) running OLD proxy.py (1718 lines, md5=06da1083) — missing R18 features
- **Evidence**: 
  - Running proxy had NO THINKING_SUPPORT dict → thinking params sent to dsv4p (bug, dsv4p doesn't support reasoning_effort)
  - Running proxy had NO tier routing → haiku→glm5.1 instead of haiku→dsv4p
  - Running proxy had NO OpenAI-style model names (gpt-4o, codex-mini-latest) → multi-agent compatibility broken
  - Running proxy only showed 2 models in /v1/models → should show 29 aliases
  - Running proxy defaults: glm5.1→41001, dsv4p→41001 — env vars override to correct backends, but defaults wrong
- **Fix**: Rebuilt both proxy containers with R18 proxy.py (1757 lines, md5=b4e099f1)
  - Now includes THINKING_SUPPORT = {"glm5.1": True, "dsv4p": False}
  - Now includes tier routing: haiku→dsv4p, opus/sonnet→glm5.1
  - Now includes 29 model aliases in /v1/models (with anthropic-version header)
  - Now includes context_window=170000 per model (safety limit for CC auto-compact)
  - Dockerfile updated from ghcr.io/berriai/litellm:v1.83.14 → litellm/litellm:v1.87.0 (locally cached, avoids ghcr.io unreachable from China)

### 2. Metrics analysis — no parameter changes warranted
- **Stable success rate**: 99.6% (Jun 11, excl startup 502s) ✅
- **TTFB p99**: 39.8s (Jun 11) — improved from Jun 10's 56.8s ✅
- **429 token quota**: 1 occurrence (transient at 21:06) — NOT configurable
- **Startup 502s**: 9 errors during container restart (15:38→15:40) — transient, not infrastructure issue
- **RPM quota healthy**: ms_requests_remaining always >1200, ms_model_requests_remaining nearly always 199
- **Container memory**: All containers at 30-35%/2GiB — R18.3 OOM fix stable ✅
- **chars/token**: 4.11 median — CPT=3.0 is appropriate (1.36x overestimate provides safety margin)
- **Conclusion**: No parameter changes warranted — all metrics stable and healthy

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
| R18.3 | glm5.1_uni41001 memory limit 1GiB→2GiB (OOM risk: 93.73%), ulimit nofile 4096→8192, CPU 1.0→2.0, reservations 512M→768M | 41001 OOM prevented ✅ |
| R19 | opc2_uname proxy rebuild with R18 proxy.py; Dockerfile→litellm:v1.87.0 (cached); metrics analysis shows all stable — no param changes | Proxy parity ✅, THINKING_SUPPORT ✅, tier routing ✅, 29 model aliases ✅ |

## 11 Immutable Variant Model IDs

**GLM-5.1 (41003/41001):** 1000 case-permutation variants of `zhipuai/glm-5.1` × 7 keys = 7000 deployments

**DSv4P (42001):** `deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`, `deepseek-ai/DeEpSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**