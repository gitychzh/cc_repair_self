# Deploy Status — opc_uname + opc2_uname (R35.6, 2026-06-21)

## Architecture (R35.5 — dispatcher + blue-green CC proxy + pure MS mode + dsv4p removed)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (model-based routing + connection-failure auto-fallback)
      ├── opus/未知 → :40005 proxy (EXPERIMENT, pure MS, interval=1.5s)
      │   [40005 连接失败 → 自动 fallback 到 40001]
      └── sonnet    → :40001 proxy (STABLE/MIRROR, pure MS, interval=1.5s)
      │   [40001 连接失败 → 自动 fallback 到 40005]

:40001/40005  cc-proxy → _cc /v1/messages → Anthropic→OpenAI 转换 → pure MS glm5.1 v×k cycling (NV disabled R35.2)
:40002        codex-proxy → _cx /v1/responses → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003        openai-proxy → _ol/_oc/_hm chat/completions → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled R35.5)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope
→ :7894 mihomo ♻️US-NV url-test (5 best US nodes) → NVIDIA integrate API (glm-5.1 unavailable; deepseek-v4-pro delisted from ModelScope)
```

## R35.5: Deepseek-V4-Pro / DSv4P Complete Removal

### Why dsv4p was removed
- ModelScope permanently delisted deepseek-v4-pro model
- All dsv4p variant IDs (10 case-variations) are dead endpoints
- 70 dsv4p LiteLLM deployments removed (140 dep → 70 dep)
- All _ol/_oc/_hm agent suffixes now route to glm5.1 backend (was routing to dsv4p since R29)
- NV API dsv4p path removed — NV only had glm-5.1 which is already unavailable

### R35.5 Changes
- **LiteLLM config.yaml**: 140→70 dep (all dsv4pv1k1~v10k7 removed)
- **All proxy config.py**: MODEL_UPSTREAMS["dsv4p"] removed, AGENT_SUFFIXES backend→glm5.1, backward compat aliases removed
- **docker-compose.yml**: LITELLM_URL_DSV4P, NUM_VARIANTS_DSV4P, MODEL_INPUT_TOKEN_SAFETY_DSV4P env vars removed
- **Agent configs**: hermes/openclaw/opencode all changed dsv4p_hm/ol/oc→glm5.1_hm/ol/oc
- **CLAUDE.md**: Architecture diagram, constraints table, agent suffix, parameters table all updated

## R35.6: OpenClaw Stuck Bug Fix (is_quota_exhaustion asymmetry + Ghost-ABORT metrics)

### Root cause: Why OpenClaw froze but Claude Code never froze
- **passthrough proxy (40003)** `is_quota_exhaustion()` used keyword matching ("quota"/"exhausted"/"insufficient"/"balance"/"limit reached")
- **cc-proxy (40001/40005)** `is_quota_exhaustion()` was already changed to `always return False` (325/331 false positives proved keywords unreliable)
- ModelScope's 429 body says "You exceeded your current quota" for RPM burst throttle — NOT actual quota exhaustion
- **Keyword match → mislabeled as `429_quota_exhausted` → `all_non_quota_429=False` → retry-after:180 → OpenClaw sees 180s → CC logic: >60s retry-after = too_long → gives up → STUCK**
- **cc-proxy → correctly `429_rate_limit` → `all_non_quota_429=True` → retry-after:5 → CC waits 5s → retries → succeeds**

### R35.6 Changes
1. **passthrough-proxy error_mapping.py**: `is_quota_exhaustion()` → always `return False` (same as cc-proxy, with R35.6 docstring explaining OpenClaw stuck root cause)
2. **cc-proxy handlers.py**: Added `_log_metrics(metrics)` to ALL error paths (ABORT, input overflow, non-cycling upstream error) — Ghost-ABORT bug fixed
3. **passthrough-proxy handlers.py**: Added `_log_metrics(metrics)` to ALL error paths (ABORT, non-cycling upstream error) — Ghost-ABORT bug fixed
4. **Effect**: All 429 errors now → `429_rate_limit` → `all_non_quota_429=True` → retry-after:5 → OpenClaw retries in 5s (was giving up at 180s)
5. **Effect**: metrics.jsonl will now correctly show ABORT events (status=429/502) instead of 100% status=200

## R35.2: Blue-Green Mirror (Both Pure MS)

### Why NV was disabled (R35.1→R35.2 evolution)
- R35.1 initial: NV_NUM_KEYS=2 on 40005, NV_NUM_KEYS=5 on 40001
- NV glm-5.1 API consistently timing out (20s timeout still fails)
- NV fallthrough wastes ~40s per request (2 keys × 20s timeout)
- NV success rate on glm-5.1: only 15% pre-R35.1, 53% post-timeout-fix (but still unreliable)
- R35.1 conclusion: disable NV for 40005 (NV_NUM_KEYS=0)
- R35.2: sync 40001 to match (NV_NUM_KEYS=0, MIN_OUTBOUND_INTERVAL_S=1.5) for lossless fallback

### R35.2 Changes
- **40001**: NV_NUM_KEYS 5→0, MIN_OUTBOUND_INTERVAL_S 2.0→1.5, NV_KEY3-5 removed
- **40005**: unchanged (already NV_NUM_KEYS=0, interval=1.5 from R35.1)
- Both containers identical config — fallback is truly lossless

### R35.2 Data (MIN_OUTBOUND_INTERVAL_S=1.5 validated on 40005)
| Metric | interval=2.0s | interval=1.5s | Change |
|--------|---------------|---------------|--------|
| avg TTFB | 10.0s | 5.0s | 2x faster |
| 429 cycling rate | 49% | 30% | -19% |
| success rate | 100% | 100% | stable |
| ABORT count | 0 | 0 | stable |
| pure MS TTFB (no cycling) | 4.0s | 3.5s | 12% faster |
| empty output | 0% | 0% | stable |

### Self-Optimization Framework (R35)
- **40005 (PRIMARY)**: Experiment container — new params/code deploy here first
- **40001 (MIRROR)**: Identical config — fallback is lossless
- **Dispatcher auto-fallback**: Connection failure → try other upstream
- **Version promotion**: When 40005 improvement validated → sync to 40001
- **Rollback**: When 40005 regresses → revert to baseline

### Key Differences Between 40005 and 40001 (R35.4: NONE)
| Aspect | 40005 (Experiment) | 40001 (Mirror) |
|--------|---------------------|-----------------|
| Build context | `./proxy/cc-proxy` | `./proxy/cc-proxy` (identical) |
| NV_NUM_KEYS | 0 | 0 (R35.2: synced) |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | 1.5 (R35.2: synced) |
| NV_TIMEOUT | 20 | 20 |
| LOG_RETENTION_DAYS | 7 | 7 (R35.4: new) |
| Logs dir | `./logs/proxy40005/` | `./logs/proxy40001/` (isolated) |
| rr_counter.json | Isolated in proxy40005 | Isolated in proxy40001 |

### Optimization Loop Tools (R35)
- `scripts/compare_proxies.sh`: Compare 40001 vs 40005 metrics (429 rate, TTFB)
- `scripts/proxy_health_score.py`: Compute health scores, write PROXY_HEALTH_SCORES.md
- `scripts/auto_tune.sh`: Apply TUNE_RULES.md parameter adjustments (bounded, safe)
- `configs/TUNE_RULES.md`: Parameter adjustment rules with safety bounds
- `configs/NEXT_ROUND.md`: Optimization round relay file
- `memory/cron-optimization-loop.md`: Detailed optimization loop procedure

## R33.2: cc-proxy Direct NV API (disabled on all ports R35.5)

### NV API Status (R35.5)
- **glm-5.1 on NV**: UNAVAILABLE (20s curl timeout, DNS errors)
- **deepseek-v4-pro on NV**: ModelScope delisted, no longer relevant
- **All ports**: NV_NUM_KEYS=0, pure MS mode only

### NV API Unsupported Parameters
- **thinking_budget**: returns 400 → proxy strips for NV calls
- **reasoning_effort**: stripped for NV calls
- **stream_options, thinking**: stripped for NV calls

### mihomo Configuration (opc_uname)
- Port 7894: ♻️US-NV url-test group (5 best US nodes, interval=60s)
- Port 7880: mixed port (general use)
- Port 7891: 🇸🇬狮城节点, 7892: 🇯🇵日本节点, 7893: ♻️US自动

## Containers (R35.5)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM | 70 glm5.1 dep (dsv4p removed R35.5) |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine |
| auth_to_api_40000 | :40000 | Dispatcher + Auto-Fallback | Routes opus→40005, sonnet→40001 |
| auth_to_api_40001 | :40001 | Proxy (cc, MIRROR) | PROXY_ROLE=cc, pure MS (NV_NUM_KEYS=0), interval=1.5s |
| auth_to_api_40002 | :40002 | Proxy (codex) | PROXY_ROLE=codex |
| auth_to_api_40003 | :40003 | Proxy (passthrough) | PROXY_ROLE=passthrough, pure MS (NV_NUM_KEYS=0 R35.5) |
| auth_to_api_40005 | :40005 | Proxy (cc, EXPERIMENT) | PROXY_ROLE=cc, pure MS (NV_NUM_KEYS=0), interval=1.5s |

## Deploy Method (R35.5)
```bash
# cc-proxy 40005 rebuild (experiment)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40005

# cc-proxy 40001 rebuild (mirror — synced config)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001

# dispatcher rebuild
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40000

# All proxy rebuild (full — needed after R35.5 dsv4p removal)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40000 auth_to_api_40001 auth_to_api_40002 auth_to_api_40003 auth_to_api_40005

# LiteLLM rebuild (70 dep — dsv4p removed)
cd /opt/cc-infra && docker restart ms_uni41001
```

## Current Parameters (R35.5)

| Parameter | Value | Container | Notes |
|-----------|-------|-----------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger |
| NV_NUM_KEYS | 0 | ALL proxies | R35.5: pure MS everywhere (NV disabled) |
| NV_TIMEOUT | 20 | all proxies | R35.1: NV-specific timeout |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | 40001/40005 | R35.2: validated (429 rate 30%) |
| MIN_OUTBOUND_INTERVAL_S | 2.0 | 40003 | unchanged |
| LOG_RETENTION_DAYS | 7 | all proxies | R35.4: auto-cleanup old logs on startup |
| UPSTREAM_TIMEOUT | 60 | all proxies | Per-key HTTPConnection timeout |
| NV_PROXY_URL | host.docker.internal:7894 | 40001/40003/40005 | Dedicated US proxy port |

## Previous History
- R30/R30.1: counter persistence + monitor.sh fix
- R31: dual CC proxy + dispatcher
- R31.4-31.9: context budget, proxy split, 429 truth, throttle
- R32: glm5.2→5.1 full repo revert
- R33.1: NV LiteLLM containers (failed)
- R33.2: cc-proxy direct NV API + MS-NV interleaving + dedicated US proxy
- R34/R34.1: passthrough-proxy NV direct API tunnel
- R35: dispatcher auto-fallback + blue-green self-optimization framework
- R35.1: NV_NUM_KEYS=0 on 40005 (NV disabled), host.docker.internal DNS fix, NV_TIMEOUT=20s
- R35.2: 40001 synced to 40005 (NV_NUM_KEYS=0, MIN_OUTBOUND_INTERVAL_S=1.5), blue-green mirror
- R35.3: (Round 3 data collection, no parameter changes)
- R35.4: Log rotation (logger.py startup cleanup, LOG_RETENTION_DAYS=7 env), stale log dirs removed
- R35.5: Complete dsv4p/deepseek-v4-pro removal (ModelScope delisted), all agents route to glm5.1 only
