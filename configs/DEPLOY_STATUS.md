# Deploy Status — opc_uname + opc2_uname (R36.5, 2026-06-23)

## Architecture (R36.5)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (auto-fallback relay, Content-Length fix, PROXY_TIMEOUT deadline)
      ├── PRIMARY  → :40005 proxy (EXPERIMENT, MS-first + NV last-resort fallback)
      └── FALLBACK → :40001 proxy (STABLE, pure MS, interval=1.5s)

:40005  cc-proxy → _cc /v1/messages → MS-first (ALL requests go to MS first)
  MS success → done (fast, ~9s avg)
  MS all-429 → NV last-resort fallback (round-robin across 5 NV keys)
  NV last-resort success → return (slow ~13-30s, but better than error)
  NV last-resort fail → ABORT-NO-FALLBACK
  NV_TIMEOUT=30s (p50=13.4s, p80=~30s → captures 80% viable NV requests)
:40001  cc-proxy → _cc /v1/messages → pure MS glm5.1 v×k cycling (NV disabled, stable baseline)
:40002  codex-proxy → _cx /v1/responses → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  openai-proxy → _ol/_oc/_hm → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."
  SSE buffer-based parsing (FR capture 85.7%, was 1.9%)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope [2GiB limit]
→ :41101-41105 LiteLLM ms_nv_4110X (1 NV key each, in-memory, 2GiB, monitoring only)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 (region-divided url-test, tolerance=0) → NVIDIA integrate API
```

## Containers (R36.5)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Content-Length fix + PROXY_TIMEOUT deadline |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, SSE buffer |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-first + NV last-resort, NV_TIMEOUT=30 |
| ms_uni41001 | :41001 | LiteLLM MS | 1CPU/2GiB | 70 glm5.1 dep (R36.3: 1→2GiB) |
| ms_nv_41101 | :41101 | LiteLLM NV K1 | 1CPU/2GiB | In-memory, 7894 proxy |
| ms_nv_41102 | :41102 | LiteLLM NV K2 | 1CPU/2GiB | In-memory, 7895 proxy |
| ms_nv_41103 | :41103 | LiteLLM NV K3 | 1CPU/2GiB | In-memory, 7896 proxy |
| ms_nv_41104 | :41104 | LiteLLM NV K4 | 1CPU/2GiB | In-memory, 7897 proxy |
| ms_nv_41105 | :41105 | LiteLLM NV K5 | 1CPU/2GiB | In-memory, 7899 proxy |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 |

## Current Parameters (R36.5)
| Parameter | Value | Scope | Notes |
|-----------|-------|-------|-------|
| contextWindow | 170000 | settings.json | CC max context |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger |
| API_TIMEOUT_MS | 600000 | settings.json | CC→proxy timeout |
| NV_NUM_KEYS | 5 | 40005 | R36.5: NV last-resort fallback (not alternating) |
| NV_NUM_KEYS | 0 | 40001/40003 | Pure MS baseline |
| NV_TIMEOUT | 30 | 40005 | R36.5: 40→30 (p50=13.4s, p80=~30s) |
| NV_PROXY_URL_MAP | {0:7894,1:7895,2:7896,3:7897,4:7899} | 40005 | Per-key proxy URL |
| NV_MAX_CYCLE | 1200000 | 40005 | Counter reset threshold |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | ALL proxies | RPM throttle (R36.3: lock-free sleep) |
| UPSTREAM_TIMEOUT | 60 | ALL proxies | Per-key HTTP timeout |
| PROXY_TIMEOUT | 600 | 40000 dispatcher | R36.3: now enforced as total deadline |
| PROXY_TIMEOUT | 300 | 40001/40005 | Overall request timeout concept |
| LOG_RETENTION_DAYS | 7 | ALL proxies | Auto-cleanup |
| STORE_MODEL_IN_DB | False | 41101-41105 | NV LiteLLM in-memory |

## R36.5 Changes (opc2_uname, 2026-06-23) — NV alternating → MS-first + NV last-resort

### 根因分析（数据驱动）
NV alternating 是纯负优化，数据证明：
- NV 成功率: 31.5% (222 attempts, 70 successes, 60 timeouts, 92 fast-fails)
- NV timeout 浪费: 27% × 40s = **40 min/day 空等**
- NV 成功延迟 p50=13.4s vs MS p50=8.9s (NV 比MS慢 1.5x 即使成功)
- MS quota 使用率: **1.3%** (14000 req/day capacity, 178 req/day actual)
- NV "免费额度"在 MS quota 98.7% 空闲时毫无价值
- R36 strict alternating 强制 41.7% slot 给 NV → **56% throughput reduction**

### 修改内容
1. **config.py `_next_variant_key_pair`**: 删除 strict alternating 逻辑 → 纯 MS round-robin
   - NV_ENABLED=True 时也走 MS-only 路径（所有请求 type=ms）
   - 消除 41.7% forced NV slots → 100% slots 给 MS
2. **upstream.py `execute_request`**: MS-first + NV last-resort
   - 删除 NV slot/MS slot 分支 → 全部走 MS-first
   - MS all-429 时尝试 NV 作为 last-resort（round-robin across 5 keys）
   - NV last-resort 新增 `_try_nv_last_resort()` 函数
   - MS 500/502/timeout → ABORT-NO-FALLBACK（NV 无济于事，只会加延迟）
3. **NV_TIMEOUT 40→30**: p50=13.4s, p80=~30s → 30s 捕获 80% viable NV requests
4. 验证: 40005 healthy ✅, MS-first 日志确认 ✅, 5 端口全部 200 ✅

### 预期效果
- **~56% throughput increase** (消除 NV timeout 浪费 + 全 slot 给 MS)
- NV last-resort 只在 MS all-429 时触发（极罕见，因为 MS quota 98.7% 空闲）
- 即使触发 NV fallback，30s timeout vs 40s 减少每超时 10s 浪费

## Deploy Method
```bash
# Step 1: sync configs
bash ~/cc_ps/cc_repair_self/scripts/sync_config.sh

# Step 2: rebuild (code changes must rebuild!)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40005

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
- R36.3: Dead code cleanup (410行), dispatcher fixes, ms_uni41001 2GiB, retry-after=5, throttle lock-free, NV_TIMEOUT 20
- R36.5: MS-first + NV last-resort (数据证明 NV alternating 是纯负优化 → 56% throughput reduction)
