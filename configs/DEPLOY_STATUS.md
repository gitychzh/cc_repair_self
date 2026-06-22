# Deploy Status — opc_uname + opc2_uname (R36.2, 2026-06-22)

## Architecture (R36.2 — dispatcher + blue-green CC proxy + MS-NV strict alternating + NV LiteLLM monitoring containers)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (auto-fallback relay + close_connection on error)
      ├── PRIMARY  → :40005 proxy (EXPERIMENT, MS-NV strict alternating, NV_NUM_KEYS=5)
      │   [40005 连接失败 → 自动 fallback 到 40001]
      └── FALLBACK → :40001 proxy (MIRROR, pure MS, interval=1.5s)
      │   [40001 连接失败 → 自动 fallback 到 40005]

:40005  cc-proxy → _cc /v1/messages → Anthropic→OpenAI 转换 → strict MS-NV alternating (ms1→nv1→ms2→nv2→ms3→nv3→ms4→nv4→ms5→nv5→ms6→nv1→ms7→nv2→...)
  NV slot: single-key attempt (no cycling), per-key proxy URL (7894-7899), NV_TIMEOUT=60s
  NV failure → immediate MS switch; MS failure → ABORT-NO-FALLBACK (no NV fallback)
  Empty 200 detection: Content-Length=0 → treated as NV failure
  Cycle counter: n+1 atomic disk write, NV_MAX_CYCLE=1200000 reset threshold
:40001  cc-proxy → _cc /v1/messages → Anthropic→OpenAI 转换 → pure MS glm5.1 v×k cycling (NV disabled, stable baseline)
:40002  codex-proxy → _cx /v1/responses → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  openai-proxy → _ol/_oc/_hm chat/completions → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope
→ :41101-41105 LiteLLM ms_nv_4110X (1 NV key each, in-memory mode, monitoring/debugging only)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 (independent url-test, 5 best US nodes per key) → NVIDIA integrate API
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

## R35.7: Stale Container Fix + Code Bug Fixes

### Stale Container Deployment (Critical)
- **Problem**: R35.5/R35.6/R35.6+ code changes were committed to git but containers were NEVER rebuilt
- **40003 passthrough-proxy**: `is_quota_exhaustion()` still using keyword matching → 140 `429_quota_exhausted` in logs → retry-after:180 still sent to OpenClaw → **R35.6 root cause still active!**
- **40002 codex-proxy**: same keyword matching bug → retry-after:30
- **All containers**: `MODEL_UPSTREAMS` still contained `dsv4p` gateway, Ghost-ABORT/Ghost-Stream fixes not deployed
- **Fix**: `sync_config.sh` + rebuild all 5 containers with `--build --force-recreate`
- **Lesson**: code commit ≠ deployment. Always sync + rebuild + smoke test after code changes.

### R35.7 Code Bug Fixes (5 bugs)
1. **PROXY_TIMEOUT NameError** (HIGH): stream.py referenced `PROXY_TIMEOUT` but didn't import it → NameError crash on stream timeout. Fixed: added `PROXY_TIMEOUT` to import in all 3 proxy stream.py files.
2. **Operator precedence** (MEDIUM): `convert_error()` / `format_openai_error_upstream()` `thinking_budget` guard only covered `invalidparameter` branch, not `range of input length` branch. Fixed: re-parenthesized to guard both branches.
3. **key_idx KeyError** (HIGH-preventive): passthrough/codex error_mapping.py + handlers.py used `a['key_idx']` directly → KeyError for NV entries. Fixed: `a.get('key_idx', a.get('nv_key_idx', 0))`.
4. **NV error type classification** (MEDIUM-preventive): `all_429`/`all_non_quota_429`/`has_conn_err` in all 3 upstream.py files missing NV error types. Fixed: added `429_nv_rate_limit`/`NVConnectionRefusedError`/`NVConnectionError`.
5. **Dispatcher close_connection** (HIGH): `_send_err()` didn't set `close_connection=True` → client reusing dead connection. Fixed: added `self.close_connection = True` + `Connection: close` header.

### 40003 Stale rr_counter Cleanup
- `{"dsv4p": 6, "glm5.1": 301}` → cleaned to `{"glm5.1": 301}` (dsv4p variant counter no longer relevant)

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

### NV API Status (R35.14)
- **glm-5.1 on NV**: ✅ RECOVERED and working (4/4 test via 7893 US proxy, 1.1-7.4s latency, content complete)
- **thinking_budget**: still returns 400 (proxy strips for NV calls)
- **opc2_uname mihomo**: 缺少7894端口（只有7891/7892/7893/7880/9090）→ NV_PROXY_URL=host.docker.internal:7894 无法工作，需配7894+US-NV proxy-group 才能重启用
- **All ports**: NV_NUM_KEYS=0, pure MS mode only (not re-enabled yet, monitoring stability 1 more round)
- **deepseek-v4-pro on NV**: ModelScope delisted, no longer relevant

### NV API Unsupported Parameters
- **thinking_budget**: returns 400 → proxy strips for NV calls
- **reasoning_effort**: stripped for NV calls
- **stream_options, thinking**: stripped for NV calls

### mihomo Configuration (opc_uname)
- Port 7894: ♻️US-NV url-test group (5 best US nodes, interval=60s)
- Port 7880: mixed port (general use)
- Port 7891: 🇸🇬狮城节点, 7892: 🇯🇵日本节点, 7893: ♻️US自动

## Containers (R36.2)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM | 70 glm5.1 dep (dsv4p removed R35.5) |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine |
| ms_nv_41101 | :41101 | NV LiteLLM K1 | In-memory, 1 NV key, monitoring only |
| ms_nv_41102 | :41102 | NV LiteLLM K2 | In-memory, 1 NV key, monitoring only |
| ms_nv_41103 | :41103 | NV LiteLLM K3 | In-memory, 1 NV key, monitoring only |
| ms_nv_41104 | :41104 | NV LiteLLM K4 | In-memory, 1 NV key, monitoring only |
| ms_nv_41105 | :41105 | NV LiteLLM K5 | In-memory, 1 NV key, monitoring only |
| auth_to_api_40000 | :40000 | Dispatcher + Auto-Fallback | Routes opus→40005, sonnet→40001 |
| auth_to_api_40001 | :40001 | Proxy (cc, MIRROR) | PROXY_ROLE=cc, pure MS (NV_NUM_KEYS=0), interval=1.5s |
| auth_to_api_40002 | :40002 | Proxy (codex) | PROXY_ROLE=codex |
| auth_to_api_40003 | :40003 | Proxy (passthrough) | PROXY_ROLE=passthrough, pure MS (NV_NUM_KEYS=0 R35.5) |
| auth_to_api_40005 | :40005 | Proxy (cc, EXPERIMENT) | PROXY_ROLE=cc, MS-NV alternating (NV_NUM_KEYS=5), interval=1.5s |

## Deploy Method (R35.7)
```bash
# IMPORTANT: Code changes require sync + rebuild (R35.7 lesson: code commit ≠ deployment)
# Step 1: sync configs from git repo to /opt/cc-infra
bash ~/cc_ps/cc_repair_self/scripts/sync_config.sh

# Step 2: rebuild containers (must use --build --force-recreate)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40000 auth_to_api_40001 auth_to_api_40002 auth_to_api_40003 auth_to_api_40005

# Step 3: verify
curl -sf http://127.0.0.1:40000/health && curl -sf http://127.0.0.1:40005/health
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40000 auth_to_api_40001 auth_to_api_40002 auth_to_api_40003 auth_to_api_40005

# LiteLLM rebuild (70 dep — dsv4p removed)
cd /opt/cc-infra && docker restart ms_uni41001
```

## R35.11 Verification Data (2.5h post-rebuild, opc_uname)

### 40005 (cc-proxy, EXPERIMENT) — 395 entries

| 指标 | 值 |
|------|-----|
| 200率 | 98.5% (389/395) |
| FR capture (200 streaming) | 100.0% (387/387) |
| 429 cycling率 (200) | 42.9% (167/389) |
| ABORT率 | 1.5% (6/395, all 429_all_transient) |
| Avg TTFB | 8108ms (median: 7096ms) |
| Avg Duration | 13721ms |

429 cycling distribution: 1-key=59, 2-key=43, 3-key=38, 4-key=16, 5-key=6, 6-key=5
RPM vs cycling: high RPM (>=3/min) → 44.5%, low RPM → 32.1% (burst throttle root cause confirmed)

### 40003 (passthrough) — 11 entries (low traffic)

| 指标 | 值 |
|------|-----|
| 200率 | 100% (11/11) |
| FR capture (200 streaming) | 87.5% (7/8) — **从 7.2% 大幅改善** |
| 429 cycling率 | 0% (0/8) |
| MSG-FIX triggers | 2 (proxy.log) |
| Avg TTFB | 7866ms |

唯一 KEY_MISSING 条目: output_tokens=0, duration=22086ms — ModelScope 边缘情况

### 40001 (MIRROR): 1 entry only (dispatcher rarely routes to it)

### ⚡ NV glm-5.1 API 发现恢复工作！
- 5/5 请求成功，finish_reason="stop"，内容完整
- 延迟: 2199ms~7880ms (avg ~5s)
- Streaming: 正常工作
- thinking_budget: 仍然 400 Unsupported (proxy 需 strip)
- **暂不重新启用 NV**：需更多稳定性数据（24-48h）

## R35.12 Verification Data (8h post-last-rebuild, opc_uname)

### 40005 (cc-proxy, EXPERIMENT) — 1182 entries (06-22 全天)

| 指标 | R35.12 (全天) | R35.11 (2.5h) | 变化 |
|------|---------------|----------------|------|
| 200率 | 99.1% (1171/1182) | 98.5% (389/395) | ↑ |
| FR capture (200 streaming) | 100.0% | 100.0% | 稳定 |
| 429 cycling率 (200) | 35.7% (418/1171) | 42.9% (167/389) | ↓ 7.2% |
| ABORT率 | 0% (0/1182) | 1.5% (6/395) | ↓ 消除 |
| Avg TTFB | 8904ms | 8108ms | ↑ 9.6%* |
| Avg Duration | 14349ms | 13721ms | ↑ 4.6% |

*TTFB上升可能来自更大的request context（更多messages/tools），非系统退化

### 40003 (passthrough) — post-rebuild FR capture re-confirmed

| 时间段 | FR capture率 | 条件 |
|--------|--------------|------|
| 旧容器 (10:xx, chunk-based) | 4/210 = **1.9%** | R35.8 chunk-based parsing |
| 新容器 (15:39+, buffer-based) | 18/21 = **85.7%** | R35.9 buffer-based parsing ✅ |

**3条 finish_reason=None 来自新容器**：全部是 ModelScope 平台截断问题（output_tokens=None, 22-45s duration, mid-stream截断不发 finish_reason/[DONE]），非 proxy bug

### NV glm-5.1 API 状态 ❌ 再次不可用
R35.11 的 5/5 成功是临时性的。3次测试全部超时（20s, 0 bytes received, TLS OK但无数据）。
确认 NV glm-5.1 不可靠，NV_NUM_KEYS=0 决策维持。

## Current Parameters (R36.2, verified on opc2_uname)

| Parameter | Value | Container | Notes |
|-----------|-------|-----------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger |
| NV_NUM_KEYS | 5 | 40005 (EXPERIMENT) | R36: 5 NV keys, strict alternating |
| NV_NUM_KEYS | 0 | 40001/40003 (STABLE) | Pure MS baseline, no NV |
| NV_TIMEOUT | 60 | 40005 | R36: increased from 20→60 for stability |
| NV_PROXY_URL_MAP | {0:7894,1:7895,2:7896,3:7897,4:7899} | 40005 | Per-key proxy URL for fault isolation |
| NV_MAX_CYCLE | 1200000 | 40005 | Cycle counter reset threshold |
| MIN_OUTBOUND_INTERVAL_S | 1.5 | ALL proxies | R35.8: ALL ports aligned to 1.5 |
| LOG_RETENTION_DAYS | 7 | all proxies | R35.4: auto-cleanup old logs on startup |
| UPSTREAM_TIMEOUT | 60 | all proxies | Per-key HTTPConnection timeout |
| PROXY_TIMEOUT | 300 | all proxies | Overall request timeout |
| NV LiteLLM | no DATABASE_URL | 41101-41105 | R36.2: in-memory mode, mihomo 7880 for GitHub, no cc_postgres dependency |

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
- R35.6: OpenClaw stuck bug fix (is_quota_exhaustion asymmetry + Ghost-ABORT metrics)
- R35.7: Stale container deployment fix + 5 code bug fixes (PROXY_TIMEOUT NameError, operator precedence, key_idx KeyError, NV classification, dispatcher close_connection)
- R35.8: 40003 throttle alignment (2.0→1.5) + passthrough null_finish metrics fix + stale dsv4p rr_counter cleanup
- R35.8+: Emergency redeployment — R35.7/R35.8 code changes were never synced to opc_uname /opt/cc-infra (third occurrence of stale-container lesson). sync_config.sh + rebuild all 5 containers verified working on both machines.
- R35.9: Passthrough SSE buffer-based parsing fix (94% finish_reason=None → 0% expected)
- R35.10: Dispatcher path fix (unify /app/gateway/ structure) + passthrough messages sequence fix (auto-append user 'Continue.' for assistant-ending sequences)
- R35.11: Verification round — SSE buffer fix verified (FR 7.2%→87.5%), MSG-FIX working (2 triggers), NV glm-5.1 API discovered working again (not yet re-enabled, monitoring stability)
- R35.12: Verification round — NV API again unavailable (R35.11 recovery confirmed transient), 40005 stable (99.1% 200, 0% ABORT, 35.7% cycling), 40003 SSE buffer fix working (85.7% FR post-rebuild vs 1.9% pre-rebuild), system stable — no changes needed
- R35.13: Verification round — NV API DNS/connectivity recovered but HTTP 429 rate-limited (no longer timeout), 40005 stable (99.1% 200, 0% ABORT, 36.5% cycling, 0% FR=None), 40003 stable (98.4% 200, 85.3% FR=None passthrough), system stable — no changes needed
- R35.14: Verification round — NV API RECOVERED (4/4 test 200, 1.1-7.4s) but opc2_uname mihomo lacks 7894 port + ModelScope DNS outage (35min → 8x ALL-500) + OpenClaw burst (5 ABORT in 2min then recovered), system fundamentally stable — no changes needed (4/5 consecutive no-change rounds)
- R36: NV re-enablement — 5 NV keys in 5 mihomo NV proxy ports (7894-7899, per-key fault isolation), strict MS-NV alternating (ms1→nv1→ms2→nv2→...), cycle counter n+1 persistent with NV_MAX_CYCLE=1200000, NV_TIMEOUT=60s, NV failure → immediate MS switch, empty 200 detection via Content-Length=0
- R36.1: NV LiteLLM containers (41101-41105) added — monitoring/debugging only, each 1 NV key with dedicated proxy port for different US IP per key
- R36.2: NV LiteLLM containers fixed — remove DATABASE_URL (in-memory mode, no DB schema creation), use mihomo mixed port 7880 for GitHub access (not NV ports 7894-7899), fix YAML merge key conflict (combine host-access + resource-1c1g into resource-1c1g-host anchor for Docker Compose v5.1.x compatibility)

## R35.9: Passthrough SSE Buffer-Based Parsing Fix

### Root Cause
- `_stream_openai_passthrough` used chunk-based line parsing: `text.split("\n")` inside each 8KB `resp.read(8192)` chunk
- SSE data lines can span 8KB chunk boundaries → broken lines never parsed as complete "data:" JSON
- finish_reason appears in the LAST SSE data line before `[DONE]` — most likely to be split across chunks
- Result: 94% of 40003 streaming responses had finish_reason=None (206/219 entries)

### Evidence
- cc-proxy `stream_to_anth` uses buffer-based parsing (`buffer += chunk.decode()` + `while "\n\n" in buffer`) → 99.6% finish_reason capture
- passthrough chunk-based parsing → 5.8% finish_reason capture (only when SSE line happens to fit within one 8KB chunk)
- finish_reason=None entries avg duration=15485ms, nonnull avg=9519ms (longer responses = more likely cross-chunk-boundary)

### R35.9 Changes
- `passthrough-proxy/gateway/handlers.py` `_stream_openai_passthrough`:
  - Added `sse_buffer = ""` line-level accumulator
  - `sse_buffer += chunk.decode()` — accumulate decoded text
  - `while "\n" in sse_buffer:` — process only complete lines
  - `sse_buffer.split("\n", 1)` — extract complete line, keep remainder in buffer
  - End-of-stream: process remaining buffer for last finish_reason
  - Passthrough behavior unchanged (raw chunk → wfile write)

### R35.8 Verification Data (2.5h post-deploy)
| Metric | 40003 (throttle=1.5, 06-22) | 40003 (throttle=2.0, 06-21) | Change |
|--------|----------------------------|----------------------------|--------|
| 200 rate | 98.2% | 100% | ⚠️ 4 ABORTs (429_all_transient) |
| 429 cycling rate | 36.1% | 35.8% | ≈ same |
| Total 429 key-cycles | 159 | 236 | ↓ 33% |
| Avg TTFB | 10273ms | 8386ms | ↑ 22%* |
| Avg Duration | 15099ms | 10466ms | ↑ 44%* |

*TTFB/Duration increase attributable to traffic volume difference (06-22: 351 combined req vs 06-21: 71 req in same time window), not throttle change.*

## R35.10: Dispatcher Path Fix + Passthrough Messages Sequence Fix

### Problem 1: `⚠️ 🛠️ docker exec auth_to_api_40000 cat /app/gateway/gateway_main.py ... grep -c "close_connection" failed`
- **Root cause**: Dispatcher Dockerfile only `COPY gateway_main.py .` → file at `/app/gateway_main.py` (no `/app/gateway/` subdirectory). Other 4 proxy containers have `COPY gateway/ ./gateway/` → file at `/app/gateway/gateway_main.py`. OpenClaw agent uses unified path `/app/gateway/gateway_main.py` for all containers → wrong path for dispatcher → cat fails → grep returns 0 → OpenClaw reports "failed"
- **Fix**: Added `COPY gateway/ ./gateway/` to dispatcher Dockerfile + created `configs/proxy/dispatcher/gateway/` subdirectory with `__init__.py` and `gateway_main.py`. Original `COPY gateway_main.py .` preserved for CMD compatibility. Both `/app/gateway_main.py` and `/app/gateway/gateway_main.py` now exist in dispatcher container.
- **Verification**: `docker exec auth_to_api_40000 cat /app/gateway/gateway_main.py | grep -c "close_connection"` now returns `3` (was `0`)

### Problem 2: `⚠️ 🛠️ run python3 inline script failed`
- **Root cause**: OpenClaw agent (GLM 5.1) generates `python3 -c "..."` commands with Python Traceback errors. This is an agent behavior quality issue, not infrastructure.
- **Status**: NOT FIXED — agent behavior problem, outside proxy scope.

### Problem 3: `Cannot continue from message role: assistant`
- **Root cause**: OpenClaw auto-compact truncates conversation history to end with an assistant role message. GLM 5.1 API (OpenAI /v1/chat/completions format) requires messages sequence to end with user/tool role. Passthrough proxy (40003) does direct body passthrough without modification → malformed sequence propagates to ModelScope → API rejects → entire session fails.
- **Fix**: Added messages sequence fix in passthrough proxy `_handle_openai_with_cycling`: if `body["messages"]` ends with `role="assistant"`, append `{"role": "user", "content": "Continue."}`. Minimal fix per OpenAI API spec requirement. Logged as `[MSG-FIX]`.
- **Verification**: curl test with assistant-ending messages → 200 OK with proper response (was "Cannot continue from message role: assistant")

### sync_config.sh Update
- Added dispatcher `gateway/__init__.py` and `gateway/gateway_main.py` entries to SYNC_MAP (was missing, causing gateway subdirectory not synced to /opt/cc-infra)
