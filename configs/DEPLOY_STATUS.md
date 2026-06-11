# Deploy Status — opc_uname + opc2_uname (R19, 2026-06-12)

## Architecture (same on both machines)
```
Agent(CC/OpenCode/Codex) → 40001/40002(proxy, format conversion + key round-robin + metrics)
    → 41003(LiteLLM glm5.1k1~k7, 7 key groups × 1000 variants = 7000 deploys) → ModelScope [PRIMARY]
    → 42001(LiteLLM dsv4pk1~k7, 7 key groups × 11 variants = 77 deploys) → ModelScope [HAiku/Mini tier]
    → 41001(LiteLLM glm5.1k1~k7, 7 key groups × 1000 variants = 7000 deploys) [BACKUP]
```

Proxy does **format conversion + key round-robin (429 cycling) + metrics logging**. No retry, no truncation, no auto-compact. LiteLLM handles per-key-group retry/fallback/routing/cooldown.

**Key Round-Robin (R19)**: Proxy cycles keys on 429 (k1→k2→...→k7), all 7 exhausted → 429 to agent. Each key's 429 attempt logged. LiteLLM config split into 7 key groups per model.

**Tier-based routing**: opus/sonnet tier → glm5.1 (7000 dep, thinking support), haiku/mini tier → dsv4p (77 dep, no thinking).

## Containers (all healthy on both machines)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| glm5.1_test41003 | :41003 | Primary glm5.1 | 7 key groups × 1000 deploys = 7000, ulimits nofile=8192, memory 2GiB |
| glm5.1_uni41001 | :41001 | Backup glm5.1 | 7 key groups × 1000 deploys = 7000, ulimits nofile=8192, memory 2GiB |
| dsv4p_uni42001 | :42001 | dsv4p | 7 key groups × 11 deploys = 77, ulimits nofile=4096, memory 2GiB |
| auth_to_api_40001 | :40001 | Proxy (opc_uname) | R19 gateway package + key round-robin ✅ |
| auth_to_api_40002 | :40002 | Proxy (opc2_uname) | R19 gateway package + key round-robin ✅ |
| cc_postgres | :5432 | LiteLLM DB | — |

## Deploy Method
```bash
# LiteLLM config change → restart only
docker restart glm5.1_test41003 / glm5.1_uni41001 / dsv4p_uni42001

# proxy change → rebuild (gateway package + Dockerfile with cached litellm image)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```
**⚠️ CRITICAL**: LiteLLM MUST be restarted BEFORE proxy when changing configs. Proxy sends `glm5.1k1`~`glm5.1k7` to LiteLLM — if LiteLLM doesn't have key groups yet, it returns "Invalid model name" → CC crash.

**Deploy order for key group changes**: 1) LiteLLM configs → 2) `docker restart` LiteLLM containers → 3) verify key groups in LiteLLM `/v1/models` → 4) proxy rebuild → 5) verify proxy `/v1/models` shows only canonical names.

## Current Parameters (R19)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Both containers running 3.0 ✅ |
| NUM_KEYS | 7 | docker-compose.yml | Key groups per model for round-robin |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Seconds |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (glm5.1/dsv4p) | 300 | litellm config.yaml | Seconds |
| num_retries (41003) | 2 | litellm config.yaml | R19: reduced from 8, proxy handles key cycling |
| num_retries (42001) | 2 | litellm config.yaml | R19: reduced from 5, proxy handles key cycling |
| cooldown_time (41003/42001) | 10 | litellm config.yaml | — |
| routing_strategy (41003/42001) | simple-shuffle | litellm config.yaml | R19: 42001 changed from latency-based |
| RateLimitErrorAllowedFails (41003/42001) | 1 | litellm config.yaml | R19: 429→proxy cycles to next key |

## Metrics Summary (06-12 opc_uname/opc2_uname, R19 deployed)

### opc_uname 40001 (from DEPLOY_STATUS R18.3)
| Metric | 06-10 | 06-11 | 06-12 (R19) | Change |
|--------|-------|-------|-------------|--------|
| Total requests | 1887 | 1555 | TBD | R19 just deployed |
| Success rate | 99.8% | 96.8% (100% excl 429) | TBD | — |
| 429 errors | 1 | 49 (token quota burst) | TBD | R19 key cycling should reduce |
| P99 TTFB | 65.0s | 49.8s | TBD | — |

### opc2_uname 40001 (R19 deployed)
| Metric | 06-09 | 06-10 | 06-11 | 06-12 (R19) | Trend |
|--------|-------|-------|-------|-------------|-------|
| Total requests | 638 | 771 | 248 | TBD | — |
| Stable success rate | 100% | 99.9% | 99.6% | TBD | — |
| 429 errors | 0 | 0 | 1 | TBD | R19 key cycling active |
| TTFB p99 | 42.5s | 56.8s | 39.8s | TBD | — |

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality

### Key Round-Robin (R19) — CRITICAL deploy order
- **LiteLLM MUST restart first**: Proxy sends `glm5.1k{N}` to LiteLLM. If LiteLLM doesn't have key groups → "Invalid model name" → CC crash
- **Deploy order**: 1) LiteLLM configs + restart → 2) Verify LiteLLM has key groups → 3) Proxy rebuild → 4) Verify proxy /v1/models only shows canonical names
- **Wrong order (what crashed opc_uname)**: proxy rebuilt first → sends `glm5.1k1` → LiteLLM only knows `glm5.1` → Invalid model → CC crash

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by ms_requests_remaining). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked). Independent from RPM.
- **R19 key round-robin addresses token quota**: Each key has independent token quota. 429 on key N → cycle to key N+1 (fresh quota). Only ALL-KEYS-429 returns 429 to agent.

### /health endpoint — NEVER use on LiteLLM
- LiteLLM /health → per-deployment checks → fd exhaustion. Use /health/liveliness.
- Proxy /health → simple status check → SAFE for Docker healthcheck.

## R19 Changes (2026-06-12)

### 1. Key round-robin architecture (PRIMARY change)
- **Problem**: 429 errors caused by token quota exhaustion per key, NOT per variant. Simple-shuffle randomly distributes across keys → single key can exhaust early. Same 7 keys across all deployments → fallback ineffective.
- **Solution**: Proxy-level key round-robin (429 cycling through all 7 keys)
  - LiteLLM configs split into 7 key groups: `glm5.1k1`~`glm5.1k7`, `dsv4pk1`~`dsv4pk7`
  - Each key group has all variants with ONE specific API key (1000 dep for glm5.1, 11 for dsv4p)
  - Proxy cycles: request N → key_idx = counter % 7 → model `{base}k{idx+1}`
  - 429 → next key (k1→k2→...→k7→k1), all 7 → return 429 to agent with retry-after=30s
  - Each 429 attempt logged via KEY-429 tag in error_detail
  - LiteLLM num_retries reduced: 8→2 (glm5.1), 5→2 (dsv4p) — proxy handles key cycling, not LiteLLM
  - RateLimitErrorAllowedFails reduced: 5→1 (glm5.1), 3→1 (dsv4p) — 429 → next key, not more retries
  - /v1/models filters key group names, only shows canonical names (glm5.1, dsv4p) to CC/agents

### 2. Gateway package structure (proxy implementation)
- Proxy implemented as Python package (`gateway/`) with config.py, handlers.py, converters.py, etc.
- Dockerfile uses `ghcr.io/berriai/litellm:v1.83.14-stable.patch.1` base image
- NUM_KEYS=7 in docker-compose.yml env vars for both proxy containers

### 3. Deploy crash lesson (CRITICAL — must remember)
- **What happened**: Deployed R19 configs on opc_uname (self) with WRONG order — proxy rebuilt first, LiteLLM not yet restarted → proxy sends `glm5.1k1` → LiteLLM returns "Invalid model name" → CC crashed
- **Lesson**: NEVER deploy config changes on your own machine first. Always deploy on remote (opc2_uname), verify stable for ≥2 hours, then update self.
- **Deploy order for key changes**: LiteLLM restart FIRST → verify key groups → proxy rebuild SECOND → verify /v1/models

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
| R19 | Key round-robin (7 key groups per model, proxy 429 cycling); LiteLLM num_retries 8→2/5→2; RateLimitErrorAllowedFails 5→1/3→1; /v1/models filters key groups; Deploy crash on opc_uname (wrong order) → lesson learned | Key cycling active ✅, /v1/models canonical only ✅ |

## 11 Immutable Variant Model IDs

**GLM-5.1 (41003/41001):** 1000 case-permutation variants of `zhipuai/glm-5.1` × 7 keys = 7000 deployments

**DSv4P (42001):** `deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`, `deepseek-ai/DeEpSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**