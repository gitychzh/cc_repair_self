# Deploy Status — opc_uname + opc2_uname (R33.2, 2026-06-20)

## Architecture (R33.2 — dispatcher + dual CC proxy + MS-NV interleaving)
```
CC (settings.json ANTHROPIC_BASE_URL=40000)
  → :40000 dispatcher (按 request body 的 model 字段路由)
      ├── opus/未知 → :40005 proxy (PROXY_ROLE=cc, primary)
      └── sonnet    → :40001 proxy (PROXY_ROLE=cc, fallback)
      → _cc /v1/messages → Anthropic→OpenAI 转换 → MS-NV interleaving
:40002          codex-proxy    → _cx /v1/responses     → Responses→Chat 转换 → MS glm5.1 v×k cycling
:40003          openai-proxy   → _ol/_oc/_hm chat/completions → OpenAI passthrough → dsv4p v×k cycling

→ :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep + dsv4pv1k1~v10k7 = 70 dep = 140 dep) → ModelScope
→ :7894 mihomo ♻️US-NV url-test (5 best US nodes) → NVIDIA integrate API (z-ai/glm-5.1, deepseek-ai/deepseek-v4-pro)
```

## R33.2: cc-proxy Direct NV API + MS-NV Interleaving + Dedicated US Proxy

### Why NV LiteLLM containers failed (R33.1)
- LiteLLM v1.87 uses aiohttp transport → ignores HTTPS_PROXY env vars
- `litellm_params.proxy` → 400 "Unsupported parameter(s)"
- Prisma DB migration via HTTP_PROXY → postgres connection through proxy → hang
- **Conclusion**: LiteLLM cannot route per-deployment proxy. Abandoned NV LiteLLM approach entirely.

### R33.2 Architecture
- **cc-proxy (40005) directly calls NV API** via HTTPS CONNECT tunnel (`http.client.HTTPSConnection.set_tunnel()`)
- **NV_PROXY_URL=http://host.docker.internal:7894** → mihomo ♻️US-NV url-test group (5 best US nodes, auto-select fastest)
- **NV calls ONLY use proxy** (no global HTTPS_PROXY → MS traffic unaffected)
- **MS calls stay on Docker cc-net** (no proxy needed, LiteLLM on same Docker network)

### MS-NV Interleaving (12-slot round-robin)
- 7 MS keys + 5 NV keys = 12 total slots
- Slot < 7 → MS (variant×key cycling via LiteLLM → ModelScope)
- Slot ≥ 7 → NV (nv_key_idx cycling via HTTPS CONNECT tunnel → NVIDIA API)
- MS all-429 → NV fallback; NV all-fail → MS fallback
- `_next_variant_key_pair()` returns 4-tuple: `(variant_idx, key_idx, upstream_type, nv_key_idx)`
- NV has NO RPM limit → 5 NV keys for cycling resilience only

### NV API Performance (tested via US proxy)
- **glm5.1 via US proxy**: 2-5s (vs 35+ seconds direct from China)
- **dsv4p via US proxy**: 2-3s
- **Best US nodes**: 圣何塞01(1.48s), 美国01(3.23s), 圣何塞02(3.55s), 洛杉矶08(4.19s), 美国03(4.89s)
- **NV API burst sensitivity**: consecutive requests slow down (need ~2-3s interval)

### NV API Unsupported Parameters
- **thinking_budget**: returns 400 "Unsupported parameter(s)" → cc-proxy strips for NV calls
- **reasoning_effort**: supported but slow (>60s timeout needed) → also stripped
- **stream_options, thinking**: stripped for NV calls

### mihomo Configuration (opc_uname)
- Port 7894: ♻️US-NV url-test group (5 best US nodes, interval=60s, tolerance=100ms)
- Port 7880: mixed port (general use, auto-select all nodes)
- Port 7891: 🇸🇬狮城节点, 7892: 🇯🇵日本节点, 7893: ♻️US自动
- Provider: nv-us-provider (filter "美国|圣何塞|阿什本|洛杉矶", 32 nodes)
- Provider URL: https://dash.pqjc.site/api/v1/pq/ad4978e7f9844ad86d770a863f61ad4b

## Containers (R33.2)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM | 70 glm5.1 + 70 dsv4p = 140 dep |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine (litellm_glm51 DB only) |
| auth_to_api_40000 | :40000 | Dispatcher | Routes opus→40005, sonnet→40001 |
| auth_to_api_40001 | :40001 | Proxy (cc, fallback) | PROXY_ROLE=cc, /v1/messages only |
| auth_to_api_40002 | :40002 | Proxy (codex) | PROXY_ROLE=codex, /v1/responses only |
| auth_to_api_40003 | :40003 | Proxy (passthrough) | PROXY_ROLE=passthrough, /v1/chat/completions only |
| auth_to_api_40005 | :40005 | Proxy (cc, primary) | PROXY_ROLE=cc, NV-enabled, NV_PROXY_URL=7894 |

## Deploy Method (R33.2+)
```bash
# cc-proxy 40005 rebuild (NV_PROXY_URL change or code change)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40005

# All proxy rebuild
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40000 auth_to_api_40001 auth_to_api_40002 auth_to_api_40003 auth_to_api_40005

# mihomo config change → reload via API
curl -X PUT http://127.0.0.1:9090/configs -H "Authorization: Bearer set-your-secret" \
  -H "Content-Type: application/json" -d '{"path":"/home/opc_uname/.config/mihomo/config.yaml"}'
# Or restart systemd service
systemctl --user restart mihomo.service
```

## R33.2 Verification (2026-06-20)
- 7 containers all healthy ✅
- NV API via 7894 (US proxy): glm5.1 200 OK, 2-5s latency ✅
- MS via LiteLLM: glm5.1 200 OK ✅
- MS-NV interleaving: slot=ms/slot=nv alternating in logs ✅
- thinking_budget stripped for NV calls ✅
- CC Anthropic format: 200 OK with thinking+text blocks ✅

## Current Parameters (R33.2)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| NV_BASEURL | https://integrate.api.nvidia.com/v1 | docker-compose.yml | NVIDIA API endpoint |
| NV_NUM_KEYS | 5 | docker-compose.yml | 5 NVIDIA API keys |
| NV_PROXY_URL | http://host.docker.internal:7894 | docker-compose.yml | Dedicated US proxy port for NV |
| MS_NV_TOTAL_SLOTS | 12 | config.py | 7 MS + 5 NV slots for interleaving |
| UPSTREAM_TIMEOUT | 60 | docker-compose.yml | Per-key HTTPConnection timeout |
| MIN_OUTBOUND_INTERVAL_S | 2.0 | docker-compose.yml | Burst throttle interval |

## NVIDIA API Keys (R33.2)
| Key | ID | Purpose |
|-----|-----|---------|
| NV_KEY1 | nv_key1_8257 | NV interleaving slot 8, 13, etc. |
| NV_KEY2 | nv_key2_2387 | NV interleaving slot 9, 14, etc. |
| NV_KEY3 | nv_key3_qq | NV interleaving slot 10, etc. |
| NV_KEY4 | nv_key4_jh | NV interleaving slot 11, etc. |
| NV_KEY5 | nv_key5_qm | NV interleaving slot 12, etc. |

NV model IDs: `z-ai/glm-5.1` (CC/Codex) | `deepseek-ai/deepseek-v4-pro` (OpenAI agents, if enabled)

## Previous History
- R30/R30.1: counter persistence + monitor.sh fix
- R31: dual CC proxy (40005 primary + 40001 fallback) + dispatcher
- R31.4-31.9: context budget, proxy split, 429 truth, throttle
- R32: glm5.2→5.1 full repo revert
- R33.1: NV LiteLLM containers (failed — proxy incompatibility)
- R33.2: cc-proxy direct NV API + MS-NV interleaving + dedicated US proxy ✅
