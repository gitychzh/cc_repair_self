# Deploy Status — opc_uname + opc2_uname (R36.3, 2026-06-22)

## Architecture (R36.3)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (auto-fallback relay, Content-Length fix, PROXY_TIMEOUT deadline)
      ├── PRIMARY  → :40005 proxy (EXPERIMENT, MS-NV strict alternating, NV_NUM_KEYS=5)
      └── FALLBACK → :40001 proxy (STABLE, pure MS, interval=1.5s)

:40005  cc-proxy → _cc /v1/messages → strict MS-NV alternating (ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→ms→nv→...)
  NV slot: single-key attempt, per-key proxy URL (7894-7899), NV_TIMEOUT=20s, sock.settimeout(NV_TIMEOUT) after conn.request()
  NV failure → immediate MS switch; MS failure → ABORT-NO-FALLBACK
:40001  cc-proxy → _cc /v1/messages → pure MS glm5.1 v×k cycling (NV disabled, stable baseline)
:40002  codex-proxy → _cx /v1/responses → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  openai-proxy → _ol/_oc/_hm → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."
  SSE buffer-based parsing (FR capture 85.7%, was 1.9%)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope [2GiB limit]
→ :41101-41105 LiteLLM ms_nv_4110X (1 NV key each, in-memory, 2GiB, monitoring only)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 (region-divided url-test, tolerance=0) → NVIDIA integrate API
```

## Containers (R36.3)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Content-Length fix + PROXY_TIMEOUT deadline |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, SSE buffer |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-NV alternating, NV_TIMEOUT=20s |
| ms_uni41001 | :41001 | LiteLLM MS | 1CPU/2GiB | 70 glm5.1 dep (R36.3: 1→2GiB) |
| ms_nv_41101 | :41101 | LiteLLM NV K1 | 1CPU/2GiB | In-memory, 7894 proxy |
| ms_nv_41102 | :41102 | LiteLLM NV K2 | 1CPU/2GiB | In-memory, 7895 proxy |
| ms_nv_41103 | :41103 | LiteLLM NV K3 | 1CPU/2GiB | In-memory, 7896 proxy |
| ms_nv_41104 | :41104 | LiteLLM NV K4 | 1CPU/2GiB | In-memory, 7897 proxy |
| ms_nv_41105 | :41105 | LiteLLM NV K5 | 1CPU/2GiB | In-memory, 7899 proxy |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 |

## Current Parameters (R36.3)
| Parameter | Value | Scope | Notes |
|-----------|-------|-------|-------|
| contextWindow | 170000 | settings.json | CC max context |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger |
| API_TIMEOUT_MS | 600000 | settings.json | CC→proxy timeout |
| NV_NUM_KEYS | 5 | 40005 | R36: strict MS-NV alternating |
| NV_NUM_KEYS | 0 | 40001/40003 | Pure MS baseline |
| NV_TIMEOUT | 20 | 40005 | R36.3: 60→30→20 (NV normal 2-5s, max 20s) |
| NV_PROXY_URL_MAP | {0:7894,1:7895,2:7896,3:7897,4:7899} | 40005 | Per-key proxy URL |
| NV_MAX_CYCLE | 1200000 | 40005 | Counter reset threshold |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | ALL proxies | RPM throttle (R36.3: lock-free sleep) |
| UPSTREAM_TIMEOUT | 60 | ALL proxies | Per-key HTTP timeout |
| PROXY_TIMEOUT | 600 | 40000 dispatcher | R36.3: now enforced as total deadline |
| PROXY_TIMEOUT | 300 | 40001/40005 | Overall request timeout concept |
| LOG_RETENTION_DAYS | 7 | ALL proxies | Auto-cleanup |
| STORE_MODEL_IN_DB | False | 41101-41105 | NV LiteLLM in-memory |

## R36.3 Changes (opc2_uname, 2026-06-22)
1. 死代码清理: 410行删除 (_try_nv_keys 180行, if False variant-fallback 190行, _is_routing_name 11行, 529死分支)
2. Dispatcher Content-Length双重注入修复 — 加入HOP集排除原始Content-Length
3. Dispatcher PROXY_TIMEOUT=600 总超时保护 — relay循环添加deadline检查
4. ms_uni41001 1GiB→2GiB — 79%→52%内存使用率，OOM风险消除
5. retry-after=10→5 (瞬态429) — 与CLAUDE.md规范对齐
6. throttle_outbound 锁外sleep — 并发请求不再排队等彼此的sleep
7. NV_TIMEOUT 60→20 — NV失败浪费从60s减到20s
8. 所有12容器healthy ✅, 5端口全部200 ✅
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
