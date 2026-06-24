# Deploy Status — opc_uname + opc2_uname (R38.11, 2026-06-24)

## Architecture (R38.9)
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
:40006  hm-proxy → _hm_nv /v1/chat/completions → deepseek primary (NVCF pexec) + 3-tier fallback
  R38.10: deepseek bypasses DEGRADING integrate API → NVCF pexec orion (ACTIVE) via SOCKS5 proxy
  kimi/glm5.1 still via LiteLLM (integrate API routes them to ACTIVE functions)
  Mixed path: deepseek → SOCKS5 → api.nvcf.nvidia.com/pexec; kimi/glm5.1 → LiteLLM → integrate API
  R38.7: deepseek RESTORED as tier 3
  R38.8: depends_on condition:service_healthy (hm40006 waits for ALL 5 LiteLLM nv_hm healthy before starting)
  R38.8: connection fast-break (2 consecutive conn errors → skip tier)
  R38.8: 408 (LiteLLM timeout) 加入 cycling 错误列表 (was: 只 cycle 429/500/502 → 408立即返回错误)
  R38.9: deepseek_hm_nv 作为 primary tier (测试延迟数据采集)
  R38.9: deepseek data collection concluded → integrate API routes deepseek to DEGRADING ai-deepseek-v4-pro → 429
  R38.10: NVCF pexec direct path for deepseek (orion ACTIVE function, bypasses DEGRADING routing)
  默认 deepseek_hm_nv(NVCF pexec) → glm5.1_hm_nv → kimi_hm_nv → 全失败 → ABORT
  TIER_TIMEOUT_BUDGET_S=60s
  fallback 从当前位置继续（不是从k1），per-tier persistent RR counter
  每个 LiteLLM 容器走各自的 mihomo per-key proxy (7894-7899) → NV API
  LiteLLM timeout=35s (sync with hm-proxy UPSTREAM_TIMEOUT=45s)
  LiteLLM drop_params=true 自动 strip NV unsupported params
  Connection:close on all requests (prevent BrokenPipe errors)
  NV_MODEL_IDS: glm5.1_hm_nv/kimi_hm_nv/deepseek_hm_nv (3-tier chain active)
  R38.8: mihomo health-check url = NV API /v1/models (not gstatic) → dead nodes detected within 3min
  R38.8: nv_proxy_selector reads mihomo API data (no self-testing), */3 cron, execution <1s
  nv_proxy_selector cron: */3 * * * * (R38.8: from */15, script now <1s, no self-testing)
  Hermes: ~/.hermes-venv/bin/hermes → config in ~/.hermes/config.yaml (R38.9: default=deepseek_hm_nv, fallback default_model=glm5.1_hm_ms)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope [2GiB limit]
→ :41101-41105 LiteLLM nv_hm_4110X (3 NV model dep each, per-key mihomo proxy → NV API)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 → NVIDIA integrate API (health-check url = NV API, interval=180s)
```

## Containers (R38.4: 7 core + 1 external + 5 HM LiteLLM = 13 total)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Content-Length fix + PROXY_TIMEOUT deadline |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, _hm_ms suffix for Hermes MS fallback |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-first + NV last-resort, NV_TIMEOUT=30 |
| hm40006 | :40006 | hm-proxy(external) | 1CPU/1GiB | R38.11: deepseek primary(NVCF pexec) + glm5.1 fallback + kimi last-resort |
| ms_uni41001 | :41001 | LiteLLM MS | 1CPU/2GiB | 70 glm5.1 dep |
| nv_hm_41101 | :41101 | LiteLLM NV HM K1 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7894 proxy |
| nv_hm_41102 | :41102 | LiteLLM NV HM K2 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7895 proxy |
| nv_hm_41103 | :41103 | LiteLLM NV HM K3 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7896 proxy |
| nv_hm_41104 | :41104 | LiteLLM NV HM K4 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7897 proxy |
| nv_hm_41105 | :41105 | LiteLLM NV HM K5 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7899 proxy |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 |

## R38 Changes (opc_uname, 2026-06-24) — R38.11 tier reorder

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

- R38.5: hm-proxy cycling throttle exemption + cooldown restore + K5 proxy fix
- R38.5 Round 2: UPSTREAM_TIMEOUT 60→45s + tier-skip when all keys cooling + nv_proxy_selector.sh→.py
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
