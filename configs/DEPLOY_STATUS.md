# Deploy Status — opc_uname + opc2_uname (R38.4, 2026-06-23)

## Architecture (R38.4)
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
:40003  passthrough-proxy → _ol/_oc/_hm_ms → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."
  _hm_ms suffix for Hermes MS fallback endpoint (R38.4: _hm_ms = Hermes + ModelScope)

── 外部 app endpoint（不属于 cc-infra 核心）──
:40006  hm-proxy → _hm_nv /v1/chat/completions → LiteLLM 41101-41105 (3-tier fallback, per-tier 5-key RR)
  默认 glm5.1_hm_nv → 全429/空200 → fallback kimi_hm_nv → 全429/空200 → fallback deepseek_hm_nv → 全失败 → ABORT
  fallback 从当前位置继续（不是从k1），per-tier persistent RR counter
  每个 LiteLLM 容器走各自的 mihomo per-key proxy (7894-7899) → NV API
  LiteLLM drop_params=true 自动 strip NV unsupported params
  NV_MODEL_IDS: glm5.1_hm_nv/kimi_hm_nv/deepseek_hm_nv (3 models, _hm_nv dual suffix R38.4, deepseek-v4-pro restored)
  Hermes: ~/.hermes-venv/bin/hermes → config in ~/.hermes/config.yaml (default=glm5.1_hm_nv R38.4)

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope [2GiB limit]
→ :41101-41105 LiteLLM ms_nv_hm_4110X (3 NV model dep each, per-key mihomo proxy → NV API)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 → NVIDIA integrate API
```

## Containers (R38.4: 7 core + 1 external + 5 HM LiteLLM = 13 total)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Content-Length fix + PROXY_TIMEOUT deadline |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, _hm_ms suffix for Hermes MS fallback |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-first + NV last-resort, NV_TIMEOUT=30 |
| hm40006 | :40006 | hm-proxy(external) | 1CPU/1GiB | Routes to LiteLLM 41101-41105 → NV API |
| ms_uni41001 | :41001 | LiteLLM MS | 1CPU/2GiB | 70 glm5.1 dep |
| ms_nv_hm_41101 | :41101 | LiteLLM NV HM K1 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7894 proxy |
| ms_nv_hm_41102 | :41102 | LiteLLM NV HM K2 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7895 proxy |
| ms_nv_hm_41103 | :41103 | LiteLLM NV HM K3 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7896 proxy |
| ms_nv_hm_41104 | :41104 | LiteLLM NV HM K4 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7897 proxy |
| ms_nv_hm_41105 | :41105 | LiteLLM NV HM K5 | 1CPU/1GiB | 3 dep (glm5.1/kimi/deepseek), per-key 7899 proxy |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 |

## R38 Changes (opc_uname, 2026-06-23) — Hermes 重新工程化

### 根因分析
Hermes 是独立全局安装的外部 app（hermes-agent 0.17.0, ~/.hermes-venv/bin/hermes），
有自己的 config（~/.hermes/config.yaml）、fallback provider、model_aliases。
R37 hm-proxy 用 HTTPS CONNECT tunnel 直连 NV API，绕过 LiteLLM。
R38 重新工程化：hm40006 改为路由到 5 个 LiteLLM 容器（41101-41105），每个容器走
各自的 mihomo per-key proxy (7894-7899)，实现 IP 多样性 + LiteLLM drop_params 支持。

关键发现：
- LiteLLM v1.87 的 HTTPS_PROXY env 对 httpx 有效（容器级代理）
- LiteLLM HM 容器曾用 DATABASE_URL 导致 "model=None" bug（DB ProxyModelTable 空）
- 修复：去掉 DATABASE_URL → STORE_MODEL_IN_DB=False（in-memory mode，从 config.yaml 读）

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
