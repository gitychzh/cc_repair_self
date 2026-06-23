# Deploy Status — opc_uname + opc2_uname (R38, 2026-06-23)

## Architecture (R38)
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
:40003  passthrough-proxy → _ol/_oc/_hm → OpenAI passthrough → MS glm5.1 v×k cycling (NV disabled)
  MSG-FIX: messages以assistant结尾→auto-append user "Continue."
  _hm suffix retained for Hermes MS fallback endpoint

── 外部 app endpoint（不属于 cc-infra 核心）──
:40006  hm-proxy → _hm /v1/chat/completions → NV-only (5 key sequential RR, HTTPS CONNECT tunnel)
  NV_MODEL_IDS: kimi_hm/glm5.1_hm/minimax_hm/deepseek_hm
  Hermes: ~/.hermes-venv/bin/hermes → config in ~/.hermes/config.yaml

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep) → ModelScope [2GiB limit]
→ :41201-41205 LiteLLM ms_nv_4110X (1 NV key each, in-memory 1GiB, monitoring only)
→ :7894-7899 mihomo ♻️US-NV-K1~K5 → NVIDIA integrate API
```

## Containers (R38: 7 core + 1 external endpoint)
| Container | Port | Role | Resources | Notes |
|-----------|------|------|-----------|-------|
| auth_to_api_40000 | :40000 | Dispatcher | 1CPU/1GiB | Content-Length fix + PROXY_TIMEOUT deadline |
| auth_to_api_40001 | :40001 | Proxy(cc,STABLE) | 1CPU/1GiB | Pure MS, NV_NUM_KEYS=0 |
| auth_to_api_40002 | :40002 | Proxy(codex) | 1CPU/1GiB | Responses→Chat |
| auth_to_api_40003 | :40003 | Proxy(passthrough) | 1CPU/1GiB | MSG-FIX, _hm suffix for Hermes fallback |
| auth_to_api_40005 | :40005 | Proxy(cc,EXPERIMENT) | 1CPU/1GiB | MS-first + NV last-resort, NV_TIMEOUT=30 |
| hm40006 | :40006 | hm-proxy(external) | 1CPU/1GiB | NV-only endpoint for Hermes agent |
| ms_uni41001 | :41001 | LiteLLM MS | 1CPU/2GiB | 70 glm5.1 dep |
| ms_nv_41101 | :41201 | LiteLLM NV K1 | 1CPU/1GiB | In-memory, 7880 proxy, monitoring |
| ms_nv_41102 | :41202 | LiteLLM NV K2 | 1CPU/1GiB | In-memory, 7880 proxy, monitoring |
| ms_nv_41103 | :41203 | LiteLLM NV K3 | 1CPU/1GiB | In-memory, 7880 proxy, monitoring |
| ms_nv_41104 | :41204 | LiteLLM NV K4 | 1CPU/1GiB | In-memory, 7880 proxy, monitoring |
| ms_nv_41105 | :41205 | LiteLLM NV K5 | 1CPU/1GiB | In-memory, 7880 proxy, monitoring |
| cc_postgres | :5432 | LiteLLM DB | 1CPU/1GiB | PostgreSQL 16 |

## R38 Changes (opc_uname, 2026-06-23) — Hermes 重新工程化

### 根因分析
Hermes 是独立全局安装的外部 app（hermes-agent 0.17.0, ~/.hermes-venv/bin/hermes），
有自己的 config（~/.hermes/config.yaml）、fallback provider、model_aliases。
R37 把它当成 cc-infra 内部组件来维护，造成架构错位：
- 5 个 NV HM LiteLLM（41101-41105）= ~3GiB RAM 零日志
- hm-proxy 代码与 passthrough-proxy 90% 重复（1028行独立 proxy）
- _hm suffix 污染 cc-proxy/codex-proxy（CC/Codex 从不处理 _hm）

### 修改内容
1. **删除 ms_nv_hm_41101-41105**: 5 个容器，3GiB RAM，零监控日志 → 完全无用
2. **删除 configs/litellm-nv-hm/**: 5 个 config YAML 文件
3. **cc-proxy/codex-proxy**: 移除 _hm suffix + glm5.1_hm mapping（CC/Codex 不用 _hm）
4. **passthrough-proxy**: 保留 _hm suffix（Hermes fallback via 40003）
5. **hm40006**: 保留，标注为"external endpoint for Hermes agent"
6. **sync_config.sh**: 移除 5 个 litellm-nv-hm 映射
7. **docker-compose.yml**: 移除 5 个 ms_nv_hm 服务 + litellm_nv_hm DB
8. **CLAUDE.md**: Hermes 明确标注为外部 app + 40006 为 external endpoint
9. **hermes-opc2_uname.yaml**: 更新为参考模板（实际配置在 ~/.hermes/config.yaml）

### 资源释放
| Item | Before | After |
|------|--------|-------|
| Docker containers | 12 (6 HM) | 7+1 (hm40006 kept) |
| RAM | ~5GiB | ~2GiB (saved ~3GiB) |
| Ports | 40006+41101-41105 | 40006 only |
| Code maintenance | hm-proxy + 5 litellm + _hm pollution | hm-proxy only |

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
- R37: Hermes专用 NV proxy hm40006 + 5 NV HM LiteLLM (41101-41105) → **R38 完全移除 LiteLLM**
- R38: Hermes 重新工程化 — 外部 app + 删除 5 无用 LiteLLM(3GiB) + 清理 _hm suffix 污染
