# Deploy Status — opc_uname + opc2_uname (R38.14, 2026-06-25)

## Architecture (R38.14: HM tier reorder — glm5.1 primary)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (auto-fallback relay, Connection:close relay, PROXY_TIMEOUT deadline)
      ├── PRIMARY  → :40005 proxy (EXPERIMENT, MS-first + NV 2-tier last-resort fallback)
      └── FALLBACK → :40001 proxy (STABLE, pure MS, Connection:close on all responses)

:40005  cc-proxy → _cc /v1/messages → MS-first (ALL requests go to MS first)
  MS success → done (fast, ~2.3s avg)
  MS all-429 → NV 3-tier last-resort fallback (R38.8: glm5.1→kimi→deepseek, all restored)
    Tier 1: glm5.1 (z-ai/glm-5.1) → all 5 NV keys RR → all-429/empty-200 →
    Tier 2: kimi (moonshotai/kimi-k2.6) → all 5 NV keys RR → all-fail →
    Tier 3: deepseek-v4-pro (deepseek-ai/deepseek-v4-pro) → all-fail → ABORT
    per-tier persistent RR counter (not restarting from k1)
    NV_TIER_TIMEOUT_BUDGET_S=90s caps total NV fallback time
    R38.8: NV conn-fast-break (2 consecutive connection errors → skip to next tier)
    Budget checked before each tier start and before each key attempt
  NV_TIMEOUT=30s (p50=13.4s, p80=~30s → captures 80% viable NV requests)
  Connection:close on all proxy responses (prevents keep-alive BrokenPipe cascade)
:40001  cc-proxy → _cc /v1/messages → pure MS glm5.1 v×k cycling (NV disabled, stable baseline)
:40002  codex-proxy → _cx /v1/responses → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003  passthrough-proxy → _ol/_oc/_hm_ms → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."
  _hm_ms suffix for Hermes MS fallback endpoint (R38.4: _hm_ms = Hermes + ModelScope)

── 外部 app endpoint（不属于 cc-infra 核心）──
:40006  hm-proxy → _hm_nv /v1/chat/completions → ALL models via NVCF pexec (SOCKS5 → ACTIVE functions)
  R38.12: ALL 3 models use NVCF pexec direct path (bypasses integrate API entirely)
    deepseek → orion-deepseek-v4-pro (ACTIVE), all params pass through ✅
    glm5.1 → ai-glm5_1 (ACTIVE), strips thinking_budget (NVCF rejects it ❌) ✅
    kimi → nvquery-kimi-k2.6 (ACTIVE), all params pass through ✅
  No LiteLLM routing — hm40006 connects directly via SOCKS5 proxy per-key mihomo
  R38.13: LiteLLM 41101-41105 containers REMOVED (no longer needed, all routing via NVCF pexec)
  默认 glm5.1_hm_nv(NVCF pexec, primary) → deepseek_hm_nv → kimi_hm_nv → 全失败 → ABORT
  TIER_TIMEOUT_BUDGET_S=60s (R38.14: budget enforced per-attempt via sock.settimeout=min(UPSTREAM_TIMEOUT, remaining_budget); MIN_ATTEMPT_TIMEOUT=10s)
  fallback 从当前位置继续（不是从k1），per-tier persistent RR counter
  NV_MODEL_IDS: glm5.1_hm_nv/kimi_hm_nv/deepseek_hm_nv (3-tier chain active)
  R38.8: mihomo health-check url = NV API /v1/models (not gstatic) → dead nodes detected within 3min
  R38.8: nv_proxy_selector reads mihomo API data (no self-testing), */3 cron, execution <1s
  Hermes: ~/.hermes-venv/bin/hermes → config in ~/.hermes/config.yaml (R38.9: default=deepseek_hm_nv, fallback default_model=glm5.1_hm_ms)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope [2GiB limit]
→ :7894-7899 mihomo ♻️US-NV-K1~K5 → NVIDIA API (health-check url = NV API, interval=180s)
```

## Containers (R38.13: 7 core + 1 external + 1 LiteLLM MS + 1 DB = 10 total)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Content-Length fix + PROXY_TIMEOUT deadline |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, _hm_ms suffix for Hermes MS fallback |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-first + NV last-resort, NV_TIMEOUT=30 |
| hm40006 | :40006 | hm-proxy(external) | 1CPU/1GiB | R38.14: glm5.1 primary + NVCF pexec all 3 models, no LiteLLM routing |
| ms_uni41001 | :41001 | LiteLLM MS | 1CPU/2GiB | 70 glm5.1 dep |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 |

## R38 Changes (opc_uname, 2026-06-24) — R38.14 HM tier reorder (glm5.1 primary)

### R40: hm-proxy fallback 根因修复 + 日志入 Postgres + 工程化 (2026-06-25)

**根因（远程 opc_uname hermes 卡住）**：远程 hm40006 容器仍跑 R38.9 旧代码（容器 2026-06-24 16:43 创建，从未 rebuild 到 R38.14），`NV_MODEL_TIERS=["deepseek","kimi","glm5.1"]`，glm5.1 在末位(idx=2)。Hermes `default_model=glm5.1_hm_nv` → `start_tier_idx=2` → `NV_MODEL_TIERS[2:]=["glm5.1"]` 只剩 1 个 tier，**无 fallback**。glm5.1 NVCF 2 次 timeout(45s+15s+connect开销=74.2s) → 直接 502 `Tiers tried: [glm5.1_hm_nv: 2×mixed]`，Hermes 持续 502 卡死。本机 6/24 08:06 也出现过同模式(tiers_tried=['glm5.1'], 71.4s)。

**代码修复**：
- **A1 环形 fallback (upstream.py execute_request)**：`tier_order = NV_MODEL_TIERS[start:] + NV_MODEL_TIERS[:start]`（环形），任何 tier 失败都有 2 个 fallback。根治"末位 tier 无 fallback"设计缺陷，不只是版本同步问题。
- **A2 budget 漏算 SOCKS5 connect (upstream.py _try_tier_keys)**：预留 `HM_CONNECT_RESERVE_S=5`，connect 后再校验 budget，read_timeout=post_connect_remaining。修复 74.2s vs 60s budget 超限。
- **A3 cooldown 误分类**：全 key cooldown 时不再 `all([])=True` 误判为 429+empty，新增 `all_cooldown` 字段。
- **A4 dead code 清理**：`tier_attempts` 过滤简化为单条件 `[a for a in ... if a.get("tier")==tier_model]`。
- **A5 错误信息增强 (error_mapping.py)**：502/429 错误体加 `tiers_tried_count` + `fallback_actually_attempted` 字段，��分"只跑1 tier"vs"全3 tier真失败"。

**日志入 Postgres（工程化）**：
- 新增 `gateway/db.py`：psycopg2 异步批量写入（后台 daemon thread，queue.Queue，2s/50条 flush，DB 挂了只写文件不降级主链路）。
- 新增 `configs/postgres/hermes-logs-schema.sql`：`hermes_logs` 库 + `hm_requests`(每请求一行) + `hm_tier_attempts`(每attempt一行) + 索引 + `v_hm_tier_health_1h`/`v_hm_key_errors_24h` 视图 + `hm_cleanup_old(30)` 清理函数。
- Dockerfile 加 `psycopg2-binary`；docker-compose hm40006 env 加 `HM_DB_*`；cc_postgres 加 `hermes_logs` 到多库 + 挂载 schema。
- 新增 `scripts/hm_log_query.sh`（recent-fails/tier-health/key-errors/single-tier-fails/request/count-by-status/tail）+ `scripts/hm_log_cleanup.sh`。
- `single-tier-fails` 是 R40 根因检测器：专门查"只跑1 tier无fallback"的失败。

**两机链路差异（系统性比对）**：
| 维度 | 本机 opc_uname (opcsname) | 远程 opc_uname (opc2sname) |
|------|--------------------------|---------------------------|
| hm40006 容器版本 | R40 (2026-06-25 02:22 rebuild) | R38.9 旧版 (2026-06-24 16:43, **未 rebuild**) |
| NV_MODEL_TIERS | [glm5.1, deepseek, kimi] (R38.14+) | [deepseek, kimi, glm5.1] (R38.9 旧序) |
| glm5.1 请求行为 | 首位，失败→fallback deepseek✅ | 末位，失败→无 fallback→502❌ |
| Hermes 状态 | 正常 | 卡住(持续 502) |
| 修复方式 | 已 R40 | 需 git pull + rebuild hm40006 |

**版本同步检查方法**：
```bash
docker exec hm40006 grep 'NV_MODEL_TIERS = ' /app/gateway/config.py
docker inspect hm40006 --format '{{.Created}}'
# R40+: tier[0]=glm5.1_hm_nv, 容器创建时间应晚于 R40 push 时间
```

### R38.14: HM tier reorder — glm5.1 primary
理由：glm5.1 中文原生优势 + Hermes社区集成成熟 + reasoning能力更强更适合agent任务，kimi k2.6智商偏低不能充分利用Hermes能力。

修改：
- hm-proxy config.py: NV_MODEL_TIERS 从 ["deepseek_hm_nv", "glm5.1_hm_nv", "kimi_hm_nv"] → ["glm5.1_hm_nv", "deepseek_hm_nv", "kimi_hm_nv"]
- hm-proxy config.py: DEFAULT_NV_MODEL 从 "deepseek_hm_nv" → "glm5.1_hm_nv"
- Hermes config.yaml: default + default_model 从 deepseek_hm_nv → glm5.1_hm_nv
- docker-compose.yml: hm40006 注释更新
- DEPLOY_STATUS.md: R38.14 变更记录

Bug fixes (R38.14):
1. **Tier budget enforcement bug**: TIER_TIMEOUT_BUDGET_S=60s was only checked BEFORE key attempts, not during.
   During NVCF overload, each key attempt takes UPSTREAM_TIMEOUT=45s → budget of 60s allowed 2 attempts (~92s total)
   before breaking, wasting ~32s beyond intended budget.
   Fix: sock.settimeout = min(UPSTREAM_TIMEOUT, remaining_budget) per-attempt.
   Also added MIN_ATTEMPT_TIMEOUT=10s threshold: skip attempt if remaining budget < 10s (avoid doomed tiny timeout).
2. **Misleading HM-TIMEOUT log**: elapsed_ms was computed from t_start (request-level start), not per-attempt start.
   A 45s per-key timeout appeared as "92080ms" (total time after 2 key attempts).
   Fix: log both attempt_elapsed_ms (per-key) and total_elapsed_ms separately.

### R38.13: LiteLLM NV HM containers removed
hm40006 logs confirm NVCF pexec is stable (77 success, 5 transient SSLEOF→retry→success, 0 ABORT, 0 tier fallback needed).
All 3 models (deepseek 247 reqs, glm5.1 48 reqs, kimi 24 reqs) route via NVCF pexec with no LiteLLM dependency.

**Removed:**
- 5 containers: nv_hm_41101~41105 (each ~600MB RAM, 1CPU, 0 traffic since R38.12)
- 5 config files: litellm-nv-hm/config-k1~k5.yaml
- 5 log dirs: /opt/cc-infra/logs/litellm-nv-hm-k1~k5
- 2 unused Docker images: litellm/litellm:v1.89.2 (1.7GB), ghcr.io/berriai/litellm:v1.83.14 (2.56GB)
- 5 service definitions from docker-compose.yml (~170 lines)

**Resource savings:** ~3GB RAM reclaimed + ~4.26GB disk reclaimed + 5 CPU slots freed. 13→10 containers.

### 根因分析 (R38.10)
integrate.api.nvidia.com/v1 路由 deepseek-ai/deepseek-v4-pro → DEGRADING ai-deepseek-v4-pro NVCF function → 429。
NVCF pexec endpoint api.nvcf.nvidia.com/v2/nvcf/pexec/functions/{id} 可直接调用 ACTIVE orion-deepseek-v4-pro → 200。
kimi/glm5.1 的 integrate API 路由到 ACTIVE functions（nvquery-kimi-k2_6, ai-glm5_1/dynamo-glm-5_1），无需 bypass。

### R38.10 修改内容
1. **hm-proxy config.py**: 新增 NVCF_PEXEC_MODELS（deepseek→orion function ID）+ HM_NV_KEYS/HM_NV_PROXY_URLS
2. **hm-proxy upstream.py**: NVCF pexec if/else分支 — deepseek用SOCKS5代理直连NVCF；kimi/glm5.1走LiteLLM
3. **hm-proxy Dockerfile**: 添加 PySocks 安装（ensurepip + pip install）
4. **docker-compose.yml**: hm40006 新增 HM_NV_KEY1-5 + HM_NV_PROXY_URL1-5 env vars
5. **Tier order**: deepseek_hm_nv 从 primary → fallback 1（kimi恢复primary），deepseek走NVCF pexec

### 修改内容
1. **hm40006 upstream.py**: 从 HTTPS CONNECT tunnel 直连 NV → 转发到 LiteLLM 41101-41105
2. **hm40006 config.py**: 新增 HM_LITELLM_URLS + HM_LITELLM_KEY + litellm_model_name()
3. **LiteLLM HM 容器**: DATABASE_URL → STORE_MODEL_IN_DB=False（修复 model=None bug）
4. **LiteLLM HM 容器**: HTTPS_PROXY 从 7880(mixed) → per-key mihomo (7894-7899)
5. **LiteLLM HM 容器**: 添加 http_proxy/https_proxy（lowercase，最大兼容）
6. **cc-proxy/codex-proxy**: 移除 _hm suffix + glm5.1_hm mapping（CC/Codex 不用 _hm）
7. **passthrough-proxy**: 保留 _hm suffix（Hermes fallback via 40003）
8. **CLAUDE.md**: Hermes 明确标注为外部 app + 40006 路由到 LiteLLM

## Deploy Method
```bash
# Step 1: sync configs
bash ~/cc_ps/cc_repair_self/scripts/sync_config.sh

# Step 2: rebuild (code changes must rebuild!)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40005 auth_to_api_40002

# Step 3: verify
curl -sf http://127.0.0.1:40000/health && curl -sf http://127.0.0.1:40005/health
curl -sf http://127.0.0.1:40006/health  # hm-proxy (Hermes endpoint)
```

## History (condensed)
- R30-31: counter persistence, dual CC proxy, dispatcher, 429 truth, throttle
- R32: glm5.2→5.1 revert
- R33-34: NV LiteLLM (failed), direct NV API tunnel
- R35: dispatcher auto-fallback, blue-green self-optimization, NV disabled (R35.1-15)
- R35.5: dsv4p permanent removal (140→70 dep)
- R35.6: OpenClaw stuck fix + Ghost metrics
- R35.7-8: 5 bug fixes, stale deploy, throttle alignment 2→1.5
- R35.9: SSE buffer parsing (FR 1.9%→85.7%)
- R35.10: dispatcher path fix + MSG-FIX
- R35.11-15: Verification → system stable (99.1%, 0% ABORT)
- R36: NV re-enablement (5-key alternating, per-key proxy, NV_TIMEOUT=60)
- R36.1: NV LiteLLM containers (41101-41105)
- R36.2: Container standardization (1CPU/1-2GiB, Docker proxy, mihomo, NV read timeout)
- R36.3: Dead code cleanup (410行), dispatcher fixes, ms_uni41001 2GiB, throttle lock-free
- R36.5: MS-first + NV last-resort (NV alternating 纯负优化 → 56% throughput reduction)
- R37: Hermes专用 NV proxy hm40006 + 5 NV HM LiteLLM (41101-41105, DATABASE_URL bug, not working)
- R38: Hermes 重新工程化 — hm40006 路由到 LiteLLM 41101-41105 + per-key mihomo + STORE_MODEL_IN_DB=False + 清理 _hm suffix
- R38.1: 清除冗余 ms_nv_41101-41105 monitoring 容器（5个，功能完全被 HM 容器覆盖），18→13容器
- R38.2: HM 3-tier fallback — minimax removed, glm5.1_hm(primary)→kimi_hm→deepseek_hm, per-tier persistent RR counter, empty-200 detection, fallback从当前位置继续
- R38.3: Model suffix _hm→_nv (NV vs MS distinction), Hermes default→glm5.1_nv, deepseek-v4-pro restored (verified via direct/US/SG proxy), sock.settimeout()读超时修复, RR counter migration _hm→_nv keys, backward compat _hm→_nv aliases
- R38.4: Dual suffix convention: _hm_nv(Hermes+NV) / _hm_ms(Hermes+MS), _nv→_hm_nv in hm-proxy, _hm→_hm_ms in passthrough-proxy, RR counter migration nv_→hm_nv_, opc_uname disk cleanup (80GB Hermes JIT .so cache removed)
- R38.5: throttle cycling豁免 + cooldown恢复 + K5代理修复 + NV per-key RPM
- R38.6: 3 CRITICAL fixes — sock.settimeout BEFORE getresponse() (infinite read timeout bug), deepseek removed from HM fallback chain (NV API unreachable, all 30s timeout), tier timeout budget 90s, KEY_COOLDOWN 30→15, MIN_OUTBOUND 3.5→1.5
- R38.7: deepseek RESTORED as tier 3 (nv_proxy_selector节点重选后3/5端口成功) + TIER_TIMEOUT_BUDGET_S 90→60s + LiteLLM timeout 60→35s (sync with hm-proxy UPSTREAM_TIMEOUT=45s) + nv_proxy_selector cron */15
- R38.8: hm40006 Connection refused storm fix — depends_on service_healthy + conn-fast-break(2 consecutive errors→skip tier) + startup-retry(wait 5s retry once for transient restarts) + cc-proxy NV conn-fast-break
- R38.8: mihomo nv-us-provider health-check url changed from gstatic→NV API /v1/models — root cause: gstatic alive nodes may be dead to NV API; NV API health-check detects dead nodes within 180s
- R38.8: nv_proxy_selector.py rewritten to read mihomo API latency data (no self-testing) — execution <1s (was 30-60s), cron */3 (was */15)
- R38.8: deepseek-v4-pro RESTORED as cc-proxy(40005) NV tier 3 fallback (tested OK: avg 1-3s, 100% success rate; R38.6 removed was deepseek-v4-flash, different model)
- R38.9: hm40006 tier order changed — deepseek_hm_nv primary → kimi_hm_nv fallback → glm5.1_hm_nv tier 3 (目的：采集 deepseek 大上下文延迟数据)
- R38.9: opc2_uname Hermes 完全复制远程部署 — hm40006 R38.9 3-tier + nv_hm_41101-41105 timeout=35s + mihomo nv-us-provider NV API health-check + Hermes v0.17.0 升级 + primary=40006(NV) + fallback=40003(MS)
- R38.9: opc2_uname Hermes Dashboard WebUI 复刻 — systemd hermes-dashboard.service (0.0.0.0:9119, --insecure) + 全 API 验证通过 + WS JSONRPC session.create ✅ + Tailscale 外部可达
- R38.10: deepseek NVCF pexec direct path — bypasses DEGRADING integrate API routing → SOCKS5 proxy → api.nvcf.nvidia.com/orion-deepseek-v4-pro (ACTIVE). kimi restored as primary. 9/9 deepseek tests succeed (avg 1.2-2.2s). HTTPS CONNECT tunnel failed (mihomo 400 Bad Request) → SOCKS5 works.
- R38.11: Tier reorder — deepseek primary (NVCF pexec 100% success, avg 1.8s) → glm5.1 fallback 1 (~20s) → kimi last-resort (~4s). NVCF pexec no longer strips thinking_budget/reasoning_effort (endpoint accepts them, tested 200 OK).
- R38.12: ALL models NVCF pexec — glm5.1 and kimi also bypass integrate API → direct SOCKS5 → NVCF ACTIVE functions. Per-model strip_params declaration (glm5.1 strips thinking_budget, deepseek/kimi pass all). LiteLLM 41101-41105 removed from active routing. hm40006 no longer depends_on LiteLLM containers. upstream.py from 836→~420 lines (deleted LiteLLM branch).
- R38.13: LiteLLM NV HM containers (41101-41105) REMOVED — stopped, removed, config files deleted, log dirs cleaned, unused Docker images pruned. 13→10 containers. ~3GB RAM + ~4.26GB disk reclaimed.
