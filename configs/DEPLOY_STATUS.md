# Deploy Status — opc_uname + opc2_uname (R36.2, 2026-06-22)

## Architecture (R36.2)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (auto-fallback relay)
      ├── PRIMARY  → :40005 proxy (EXPERIMENT, MS-NV strict alternating, NV_NUM_KEYS=5)
      └── FALLBACK → :40001 proxy (STABLE, pure MS, interval=1.5s)

:40005  cc-proxy → _cc /v1/messages → strict MS-NV alternating (ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→...)
  NV slot: single-key attempt, per-key proxy URL (7894-7899), NV_TIMEOUT=60s, sock.settimeout(NV_TIMEOUT) after conn.request()
  NV failure → immediate MS switch; MS failure → ABORT-NO-FALLBACK
:40001  cc-proxy → _cc /v1/messages → pure MS glm5.1 v×k cycling (NV disabled, stable baseline)
:40002  codex-proxy → _cx /v1/responses → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  openai-proxy → _ol/_oc/_hm → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."
  SSE buffer-based parsing (FR capture 85.7%, was 1.9%)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope
→ :41101-41105 LiteLLM ms_nv_4110X (1 NV key each, in-memory, 2GiB, monitoring only)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 (region-divided url-test, tolerance=0) → NVIDIA integrate API
```

## Containers (R36.2)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Routes opus→40005 |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, SSE buffer |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-NV alternating, NV_NUM_KEYS=5 |
| ms_uni41001 | :41001 | LiteLLM MS | 1CPU/1GiB | 70 glm5.1 dep |
| ms_nv_41101 | :41101 | LiteLLM NV K1 | 1CPU/2GiB | In-memory, 7894 proxy |
| ms_nv_41102 | :41102 | LiteLLM NV K2 | 1CPU/2GiB | In-memory, 7895 proxy |
| ms_nv_41103 | :41103 | LiteLLM NV K3 | 1CPU/2GiB | In-memory, 7896 proxy |
| ms_nv_41104 | :41104 | LiteLLM NV K4 | 1CPU/2GiB | In-memory, 7897 proxy |
| ms_nv_41105 | :41105 | LiteLLM NV K5 | 1CPU/2GiB | In-memory, 7899 proxy |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 |

## Current Parameters (R36.2)
| Parameter | Value | Scope | Notes |
|-----------|-------|-------|-------|
| contextWindow | 170000 | settings.json | CC max context |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger |
| API_TIMEOUT_MS | 600000 | settings.json | CC→proxy timeout |
| NV_NUM_KEYS | 5 | 40005 | R36: strict MS-NV alternating |
| NV_NUM_KEYS | 0 | 40001/40003 | Pure MS baseline |
| NV_TIMEOUT | 60 | 40005 | sock.settimeout after conn.request() |
| NV_PROXY_URL_MAP | {0:7894,1:7895,2:7896,3:7897,4:7899} | 40005 | Per-key proxy URL |
| NV_MAX_CYCLE | 1200000 | 40005 | Counter reset threshold |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | ALL proxies | RPM throttle |
| UPSTREAM_TIMEOUT | 60 | ALL proxies | Per-key HTTP timeout |
| PROXY_TIMEOUT | 300 | ALL proxies | Overall request timeout |
| LOG_RETENTION_DAYS | 7 | ALL proxies | Auto-cleanup |
| STORE_MODEL_IN_DB | False | 41101-41105 | NV LiteLLM in-memory |

## R36.2 Verification (opc2_uname, 2026-06-22)
- 12/12 containers healthy ✅
- MS-NV alternating: ms→nv→ms→nv confirmed (8 consecutive 200) ✅
- All 5 NV ports: k1(7894)=2.7-6.8s, k2(7895)=10.5-16.9s, k3(7896)=1.9-3.3s, k4(7897)=3.4-8s, k5(7899)=5-6.4s ✅
- 40001 baseline + dispatcher fallback ✅
- NV LiteLLM memory: 47%/2GiB (OOM resolved) ✅

## Deploy Method
```bash
# Step 1: sync configs
bash ~/cc_ps/cc_repair_self/scripts/sync_config.sh

# Step 2: rebuild (code changes must rebuild!)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40000 auth_to_api_40001 auth_to_api_40002 auth_to_api_40003 auth_to_api_40005

# Step 3: verify
curl -sf http://127.0.0.1:40000/health && curl -sf http://127.0.0.1:40005/health
```

## History (condensed)
- R30-31: counter persistence, dual CC proxy, dispatcher, 429 truth, throttle
- R32: glm5.2→5.1 revert
- R33-34: NV LiteLLM (failed), direct NV API tunnel
- R35: dispatcher auto-fallback, blue-green self-optimization, NV disabled (R35.1-15)
- R35.5: dsv4p permanent removal (140→70 dep)
- R35.6: OpenClaw stuck fix (is_quota_exhaustion→always False) + Ghost metrics
- R35.7-8: 5 bug fixes, stale deploy (3 occurrences), throttle alignment 2→1.5
- R35.9: SSE buffer parsing (FR 1.9%→85.7%)
- R35.10: dispatcher path fix + MSG-FIX
- R35.11-15: Verification rounds → system stable (99.1%, 0% ABORT)
- R36: NV re-enablement (5-key alternating, per-key proxy, NV_TIMEOUT=60)
- R36.1: NV LiteLLM containers (41101-41105)
- R36.2: Container standardization (1CPU/1-2GiB, Docker proxy, mihomo region-divided, NV read timeout fix, 2GiB NV LiteLLM)
