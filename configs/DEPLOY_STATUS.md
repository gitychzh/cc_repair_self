# Deploy Status — opc_uname + opc2_uname (R23, 2026-06-12)

## Architecture (R23)
```
Agent(CC/OpenCode/Codex) → 40001/40002(proxy, format conversion + variant×key 2D round-robin + metrics)
    → 41001 ms_uni41001 LiteLLM (glm5.1v1k1~v10k7 + dsv4pv1k1~v10k7 = 140 deploys) → ModelScope [UNIFIED]
```

Proxy does **format conversion + variant×key 2D round-robin + error cycling (429/500/502) + metrics logging**. No retry, no truncation, no auto-compact. Proxy precisely specifies variant+key combo — LiteLLM does NOT do routing, just forwards.

**Variant×Key 2D Round-Robin (R21)**:
- request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- → model name: `{base}v{V}k{K}` (e.g. glm5.1v1k1, dsv4pv3k5)
- Error cycling (429/500/502): same variant, next key (k→k+1). All 7 keys failed → classify and return to agent (all-429→rate_limit; has-500/502→api_error; has-timeout→502)
- Each variant has independent 200/id/day quota on ModelScope

**Tier-based routing**: opus/sonnet tier → glm5.1 (70 dep, thinking support), haiku/mini tier → dsv4p (70 dep, no thinking).

## Containers (R23)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| ms_uni41001 | :41001 | Unified LiteLLM | 14 groups × 10 variants = 140 dep, ulimits nofile=2048, memory 1GiB |
| auth_to_api_40001 | :40001 | Proxy (opc_uname) | R21 variant×key 2D round-robin → ms_uni41001 |
| auth_to_api_40002 | :40002 | Proxy (opc2_uname) | R21 variant×key 2D round-robin → ms_uni41001 (NOT YET DEPLOYED on opc2_uname) |
| cc_postgres | :5432 | LiteLLM DB | PostgreSQL 16-alpine |

**R23: Removed containers 41003 (glm5.1_test41003) and 42001 (dsv4p_uni42001)** — these were retained but NOT routed since R21. ms_uni41001 is the sole upstream for both models. No fallback containers needed.

## Deploy Method (R21+)
```bash
# ms_uni41001 config change → restart only
docker restart ms_uni41001

# proxy change → rebuild (need new Dockerfile build)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```

**⚠️ CRITICAL R21 Deploy Order**: ms_uni41001 container must be running with R21 config BEFORE proxy is rebuilt. Proxy sends `glm5.1v1k1` etc to LiteLLM — if LiteLLM doesn't have these model names → "Invalid model name" → CC crash.

**Deploy order for R21 on opc_uname**:
1. Copy new litellm-glm51/config.yaml → /opt/cc-infra/litellm-glm51/config.yaml
2. Copy new docker-compose.yml → /opt/cc-infra/docker-compose.yml
3. Copy new proxy.py → /opt/cc-infra/proxy/proxy.py
4. Start ms_uni41001: `cd /opt/cc-infra && docker compose up -d ms_uni41001`
5. Wait for ms_uni41001 to become healthy: `docker ps` check
6. Rebuild proxy: `cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002`
7. Verify: curl test glm5.1 and dsv4p, check /v1/models

**⚠️ opc2_uname NOT YET DEPLOYED** — will only deploy after opc_uname proven stable for ≥2 hours.

**opc_uname R21 DEPLOYED 2026-06-12 13:40 CST**: All containers healthy. Curl test glm5.1+dsv4p return 200. /v1/models shows canonical names only. Metrics confirm variant_idx+key_idx in v×k 2D round-robin logs.

## Current Parameters (R23)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | docker-compose.yml | Both containers running 3.0 ✅ |
| NUM_KEYS | 7 | docker-compose.yml | Keys per model for round-robin |
| NUM_VARIANTS_GLM51 | 10 | docker-compose.yml | R21: variants per key group for glm5.1 |
| NUM_VARIANTS_DSV4P | 10 | docker-compose.yml | R21: variants per key group for dsv4p (was 11) |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Seconds |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (ms_uni41001) | 300 | litellm config.yaml | Seconds |
| num_retries (ms_uni41001) | 0 | litellm config.yaml | R22: proxy handles all error cycling; LiteLLM pure pass-through |
| cooldown_time (ms_uni41001) | 10 | litellm config.yaml | — |
| routing_strategy (ms_uni41001) | simple-shuffle | litellm config.yaml | Proxy specifies exact model, LiteLLM just forwards |
| RateLimitErrorAllowedFails | 0 | litellm config.yaml | R22: 429 cycling by proxy, LiteLLM no retry (avoid wasting quota) |
| TimeoutErrorAllowedFails | 0 | litellm config.yaml | R22: timeout cycling by proxy |
| InternalServerErrorAllowedFails | 0 | litellm config.yaml | R22: 500/choice:null cycling by proxy |
| API_TIMEOUT_MS | 600000 | settings.json | R22: CC→proxy HTTP total timeout (5min→10min) |

## opc2_uname Link Verification (R22, 2026-06-12)

**opc2_uname 所有配置与仓库完全一致** ✅：
- proxy.py: md5=40426e02d9f6fd4913395e5e501c04a4 (local=repo)
- docker-compose.yml: diff=0 (local=repo)
- litellm-glm51/config.yaml: diff=0 (local=repo)
- 4个容器全部 healthy (ms_uni41001, cc_postgres, auth_to_api_40001, auth_to_api_40002)
- Proxy env vars confirmed: NUM_KEYS=7, NUM_VARIANTS_GLM51=10, NUM_VARIANTS_DSV4P=10, PROXY_TIMEOUT=300
- LiteLLM env vars confirmed: 7 MS_KEYs, MS_BASEURL, DATABASE_URL all present
- curl test glm5.1 via 40001 returns 200 ✅

**⚠️ opc2_uname 本机settings.json API_TIMEOUT_MISMATCH**：
- 本机 `~/.claude/settings.json`: API_TIMEOUT_MS=300000 (旧值，5min)
- 仓库 `configs/claude/settings-opc2_uname.json`: API_TIMEOUT_MS=600000 (R22新值，10min)
- **需要同步**: opc2_uname下次deploy时必须更新此值，否则极端7-key cycling场景可能超时

**opc_uname 可达 via tailscale**: SSH `opc2_uname@100.109.57.26:222` ✅。LAN 192.168.1.104/105 也可用但不太稳定。

## Log System Analysis (R22, 2026-06-12)

### Proxy日志（3层日志系统）

| 日志层 | 文件格式 | 内容 | 大小趋势 |
|--------|----------|------|----------|
| proxy.{date}.log | 纯文本 | 每请求一行简要日志（REQ/ERR/TIMEOUT等） | 0.2-0.6MB/天 |
| metrics.{date}.jsonl | JSON行 | 结构化metrics：request_id, model, ttfb_ms, tokens, variant_idx, key_idx | 0.2-2.5MB/天 |
| error_detail.{date}.jsonl | JSON行 | 详细错误：error_subcategory, upstream_error_body, key_cycle_attempts | 0-0.35MB/天 |

**proxy 40001 logs**: 12MB总计（10天有数据，06-06/07/08空缺=proxy重建期间）
**proxy 40002 logs**: 672KB总计（9天连续数据）

### Docker容器日志

- json-file driver: max-size=50m, max-file=5, tag=container/{{.Name}}
- 自动rotation ✅，每个容器最多250MB

### LiteLLM日志

- `/opt/cc-infra/logs/litellm-glm51/`: 空目录（LiteLLM日志写入容器内/app/logs/，volume挂载但无日志文件）
- **问题**: LiteLLM litellm_settings.json_logs=true，但日志文件未出现在挂载目录中。可能是LiteLLM写入PostgreSQL而非文件。

### ⚠️ 缺失：Proxy日志无自动清理机制

- proxy.py按日期写文件（proxy.{date}.log, metrics.{date}.jsonl, error_detail.{date}.jsonl）
- **无rotation/purge/cleanup**: 日志文件永不删除
- Docker容器日志有50m×5 rotation ✅，但proxy自己的应用日志无任何清理
- **建议**: 添加crontab任务，保留最近7天proxy日志，删除7天前的日志文件
- 当前影响: 12MB/10天 ≈ 1.2MB/天，6个月约216MB。不紧急但需关注。

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality

### Variant×Key 2D Round-Robin (R21) — CRITICAL deploy order
- **ms_uni41001 MUST be running first**: Proxy sends `glm5.1v1k1` to LiteLLM. If LiteLLM doesn't have these names → "Invalid model name" → CC crash
- **Deploy order**: 1) ms_uni41001 config + start → 2) Verify LiteLLM has v+k model names → 3) Proxy rebuild → 4) Verify proxy /v1/models only shows canonical names

### Single point of failure (R21 risk)
- Both glm5.1 and dsv4p route to the same ms_uni41001 container
- If ms_uni41001 crashes → BOTH models unavailable
- **Mitigation**: ms_uni41001 has been stable since R21 deploy. If it fails, proxy env vars can be changed to route to a new LiteLLM container on any port.

### ModelScope dual quota system
- **RPM quota**: 200/id/day per variant (tracked by ms_requests_remaining). Resets daily.
- **Token quota**: Per-key hourly/daily token allocation (NOT tracked). Independent from RPM.

### /health endpoint — NEVER use on LiteLLM
- LiteLLM /health → per-deployment checks → fd exhaustion. Use /health/liveliness.
- Proxy /health → simple status check → SAFE for Docker healthcheck.

## R23 Changes (2026-06-12)

### 1. R21 gateway code deployed to opc_uname container (opc_uname push)
- **Issue**: Remote opc_uname container was running R19 gateway code (key-only round-robin, `glm5.1k1~k7` format)
- **Root cause**: Docker container image was stale — R21 code was on disk but container wasn't rebuilt
- **Fix**: `docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002`
- **Verified**: Logs show `v{V}k{K}` format, NUM_VARIANTS={glm5.1:10, dsv4p:10}

### 2. PROXY_TIMEOUT=2s timeout cycling test (opc_uname push)
- **Purpose**: Verify that socket.timeout correctly triggers key cycling (R21 feature)
- **Test**: Changed PROXY_TIMEOUT from 300s to 2s, observed gateway logs for ~10 minutes
- **Results**: ✅ All key cycling features verified (socket.timeout captured, k→k+1 cycling, 502 api_error to CC)
- **PROXY_TIMEOUT restored to 300s** after test completed

### 3. SSH connection updated (opc_uname push)
- **opc_uname SSH**: Changed from `192.168.1.104:222` to `100.109.57.26:222` (tailscale IP)

### 4. Removed 41003/42001 containers (opc2_uname push)
- **These containers were retained but NOT routed since R21** — no traffic went through them
- ms_uni41001 handles all glm5.1 + dsv4p traffic as sole upstream
- **Removed from docker-compose.yml**: 6 containers → 4 containers (cc_postgres, ms_uni41001, auth_to_api_40001, auth_to_api_40002)
- **Deleted config files**: `configs/litellm-glm51-test/config.yaml` (839 lines), `configs/litellm-dsv4p/config.yaml` (923 lines)
- **Removed PostgreSQL databases**: litellm_glm51_test and litellm_dsv4p from POSTGRES_MULTIPLE_DATABASES
- **Updated scripts/docs**: Removed all 41003/42001 references

## R22 Changes (2026-06-12)

### 1. Proxy error cycling expanded: 429 → 429+500+502
- **ModelScope deducts quota for every request**, even errors (429, choice:null/500, 502)
- **New behavior**: 429, 500, 502 all cycle to next key (same variant, k→k+1)
- **All-keys-exhausted classification**: all-429→rate_limit_error; has-500→api_error; has-502→api_error; has-timeout→502 api_error
- **Not cycling**: 400 input overflow, 400 inappropriate content, 400 thinking_budget, 401/403 auth errors

### 2. LiteLLM num_retries=0, all allowed_fails=0
- **R21 architecture**: each model_name has exactly 1 deployment → LiteLLM has NO fallback to try
- **New values**: all 0 → LiteLLM is pure pass-through, proxy handles all cycling

### 3. CC API_TIMEOUT_MS: 300000→600000 (5min→10min)
- **CC SDK default**: 600000ms (10 min) — we now match it

**R22 DEPLOYED on opc_uname 2026-06-12 15:20 CST**: Proxy rebuilt + LiteLLM restarted + CC settings updated.

## R21 Changes (2026-06-12)

### 1. Unified container ms_uni41001 (PRIMARY change)
- **14 key groups**: 7 glm5.1 groups (k1~k7 × v1~v10 = 70 dep) + 7 dsv4p groups (k1~k7 × v1~v10 = 70 dep)
- **model_name format**: `{base}v{V}k{K}` (e.g. glm5.1v1k1, dsv4pv3k5)
- **dsv4p reduced from 11→10 variants**: Removed `deepseek-ai/DeEpSeek-V4-Pro` per user decision

### 2. Proxy variant×key 2D round-robin (R21)
- **2D counter**: request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
- **Error cycling (429/500/502)**: same variant, cycle to next key (k→k+1)

### 3. All routing → ms_uni41001 (single upstream)
- Both glm5.1 and dsv4p now route to ms_uni41001

### 4. Resource adjustment for ms_uni41001
- nofile: 8192→2048 (140 dep vs old 7000)
- memory: 2GiB→1GiB
- CPU: 2→1
- start_period: 180→60s

## Parameter Change History (condensed)

| Round | Changes | Result |
|-------|---------|--------|
| R1-5 | cooldown params, socket bug, conn_retry removal, num_retries=5 | 85.4%→100% |
| R12 | Removed proxy auto-compact; safety 120K→170K; contextWindow 120K→170K | 80%→97% |
| R7 | CHARS_PER_TOKEN 2.0→3.0; safety 170K→190K; contextWindow 170K→190K | 99.6% |
| R15 | compactWindow 180K→140K; contextWindow/safety 190K→170K | 99.8% |
| R16 | compactWindow 140K→155K (CC overestimation 1.7x) | 99.8% best ever |
| R17 | opc2_uname full sync: num_retries 30→8 + settings.json 155K + proxy.py parity | 99.8%+ stable |
| R18 | Tier-based routing + THINKING_SUPPORT + haiku→dsv4p + gateway package | 100% success |
| R18.1-3 | Metrics analysis; dsv4p memory 2GiB; glm5.1_uni41001 memory 2GiB | OOM prevented ✅ |
| R19 | Key round-robin (7 groups per model, 429 cycling); num_retries 8→2/5→2 | Key cycling ✅ |
| R19.1 | socket.timeout单独捕获 + timeout_exceeded_by_ms + 全key失败分类 | No timeout events yet |
| R20 | 41003 variant reduction 1000→10; resource savings | Deploying, verified ✅ |
| R21 | Unified ms_uni41001 (140 dep glm5.1+dsv4p); variant×key 2D round-robin; dsv4p 11→10 variants | DEPLOYED ✅ |
| R22 | Proxy 429+500+502 error cycling; LiteLLM num_retries=0 all allowed_fails=0; CC API_TIMEOUT_MS 600000 | DEPLOYED ✅ |
| R23 | opc_uname: R21 gateway deployed+timeout cycling test ✅; opc2_uname: removed 41003/42001 containers+configs+refs | Config cleanup + gateway verified ✅ |

## 10 Variant Model IDs (ms_uni41001, R21)

**GLM-5.1 (ms_uni41001):** 10 variants × 7 keys = 70 deployments
`ZHIPUAI/GLM-5.1`, `ZHIPUAI/GLm-5.1`, `ZHIPUAI/GlM-5.1`, `ZHIPUAI/Glm-5.1`, `ZHIPUAI/gLM-5.1`, `ZHIPUAI/gLm-5.1`, `ZHIPUAI/glM-5.1`, `ZHIPUAI/glm-5.1`, `ZHIPUAi/GLM-5.1`, `ZHIPUAi/GLm-5.1`

**DSv4P (ms_uni41001):** 10 variants × 7 keys = 70 deployments (was 11, v11 removed per R21)
`deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**

**Removed**: `deepseek-ai/DeEpSeek-V4-Pro` (dsv4p v11) — per user decision R21, losing 7×200=1400 req/day dsv4p capacity.