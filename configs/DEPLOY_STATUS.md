# Deploy Status — opc_uname (R17, 2026-06-11)

## Architecture
```
CC → 40001(proxy) → 41003(LiteLLM glm5.1, 1000 variants × 7 keys = 7000 deploys) → ModelScope
                    → 42001(LiteLLM dsv4p, 11 variants × 7 keys = 77 deploys) → ModelScope
                    → 41001(LiteLLM glm5.1-backup, 1000 variants × 7 keys) [BACKUP]
```

Proxy does **format conversion + force-stream + stream_usage + metrics logging only**. No retry, no truncation, no auto-compact. LiteLLM handles retry/fallback/routing/cooldown.

## Containers (all healthy)
| Container | Port | Role | Notes |
|-----------|------|------|-------|
| glm5.1_test41003 | :41003 | Primary glm5.1 | 7000 deploys, ulimits nofile=8192 |
| glm5.1_uni41001 | :41001 | Backup glm5.1 | 7000 deploys, ulimits nofile=4096 |
| dsv4p_uni42001 | :42001 | dsv4p | 77 deploys, ulimits nofile=4096 |
| auth_to_api_40001 | :40001 | Proxy (opc_uname) | Format conversion + stream_usage + metrics |
| auth_to_api_40002 | :40002 | Proxy (opc2_uname) | Same codebase |
| cc_postgres | :5432 | LiteLLM DB | — |

## Deploy Method
```bash
# LiteLLM config change → restart only
docker restart glm5.1_uni41001 / dsv4p_uni42001

# proxy.py change → rebuild
cd /opt/cc-infra && DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001

# Full rebuild
cd /opt/cc-infra && docker compose up -d --force-recreate

# CC restart
bash ~/cc_ps/cc_recover/restart_claude.sh
```
Docker Hub unreachable from China → mihomo on :7890 as Docker systemd proxy. `DOCKER_BUILDKIT=0` required (BuildKit ignores systemd proxy).

## Current Parameters (R17)

| Parameter | Value | File | Notes |
|-----------|-------|------|-------|
| contextWindow | 170000 | settings.json | CC max context tracking |
| autoCompactWindow | 155000 | settings.json | CC auto-compact trigger threshold |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | settings.json env + .bashrc + .profile | Env var backup for CC |
| MODEL_INPUT_TOKEN_SAFETY | 170000 | docker-compose.yml | Reported to CC via /v1/models |
| CHARS_PER_TOKEN_ESTIMATE | 3.0 (docker-compose) | docker-compose.yml | opc_uname running container still 2.0 (needs recreate); opc2_uname running 3.0 ✅ |
| PROXY_TIMEOUT | 300 | docker-compose.yml | Seconds |
| MAX_TOOL_DESC | 2000 | docker-compose.yml | Characters |
| MAX_SCHEMA_DESC | 600 | docker-compose.yml | Characters |
| timeout (glm5.1/dsv4p) | 300 | litellm config.yaml | Seconds |
| num_retries (41003) | 8 | litellm config.yaml | — |
| num_retries (42001) | 5 | litellm config.yaml | — |
| cooldown_time (41003) | 10 | litellm config.yaml | — |
| cooldown_time (42001) | 30 | litellm config.yaml | — |
| routing_strategy (41003) | simple-shuffle | litellm config.yaml | — |
| routing_strategy (42001) | latency-based-routing | litellm config.yaml | — |
| RateLimitErrorAllowedFails (41003) | 5 | litellm config.yaml | — |
| RateLimitErrorAllowedFails (42001) | 3 | litellm config.yaml | — |

## Metrics Summary (06-10, latest full day)

| Metric | 40001 (opc_uname) | 40002 (opc2_uname) |
|--------|-------------------|---------------------|
| Total requests | 1887 | 48 |
| Success rate | 99.8% | 100% |
| Errors | 2×502 timeout, 1×429 quota | 0 |
| Avg latency | 20.7s | 6.2s |
| P50 latency | 17.0s | 5.0s |
| P90 latency | 35.8s | — |
| P99 latency | 80.4s | — |
| Unique deployments used | 1660/7000 | — |
| Quota remaining | 150-199 (all healthy) | — |

**06-11 so far**: 17 requests, 100% success.

## Historical Trend

| Day | Total | Success | Avg Latency | Notes |
|-----|-------|---------|-------------|-------|
| 06-02 | 243 | 80.2% | ~14s | Pre-R12 (proxy auto-compact) |
| 06-03 | 1214 | 84.2% | ~14s | 47 InputExceedsProxyReject |
| 06-05 | 1558 | 80.7% | ~14s | Pre-R12 |
| 06-09 | 220 | 96.8% | 13.9s | Post-R12, startup errors |
| 06-10 | 707 | 99.6% | 19.3s | Post-R7 |
| 06-10 | 1887 | 99.8% | 20.7s | Post-R15/R16, best ever |

## Key Issues & Notes

### CC auto-compact behavior (CRITICAL for IQ preservation)
- **Auto-compact uses `stripNonEssential=true`**: truncates tool output, removes tool defs → low-quality summary
- **Manual `/compact` uses `stripNonEssential=false`**: full context + all tools → much better summary
- **When CC warns "Autocompact will trigger soon"**, proactively run `/compact <focus>` for better quality
- **CC overestimation 1.7x**: at autoCompactWindow=155K trigger, median real tokens ≈ 91K (45% capacity)
- Write critical info to CLAUDE.md/memory — these survive compaction

### CHARS_PER_TOKEN_ESTIMATE discrepancy (opc_uname only)
- docker-compose.yml = 3.0, but opc_uname running container = 2.0 (container was restarted, not recreated)
- Only affects metrics logging (estimated_input_tokens calculation), NOT CC behavior
- opc2_uname running container = 3.0 ✅ (fully synced in R17)
- Container recreate needed on opc_uname: `docker compose up -d --force-recreate auth_to_api_40001`

### CC v2.1.170 startup connectivity check
- Uses **shell env vars** (ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, HTTPS_PROXY), NOT settings.json
- Three-layer persistence: .bashrc (before non-interactive return) + .profile (login shells) + restart_claude.sh (`bash --login -c`)
- Without shell env vars → CC connects api.anthropic.com → 401 → refuses to start

### Proxy NEVER truncates/compacts (R12 principle)
- Proxy-level truncation causes catastrophic context loss ("completely forgets everything")
- CC built-in auto-compact is sole mechanism — same outcome but CC's own decision
- Input overflow → invalid_request_error (CC stops, user starts new conversation)

### ModelScope limits
- Input token limit: 202,745 (confirmed by ModelScope error)
- Quota: 200 requests/id/day per variant × 1000 variants = 200K/day theoretical max
- Daily quota resets; 429 insufficient_quota is genuine exhaustion, not config fixable

### /health endpoint — NEVER use /health for monitoring
- Use /health/liveliness only. /health triggers per-deployment checks → fd exhaustion.

## Parameter Change History (condensed)

| Round | Changes | Result |
|-------|---------|--------|
| R1-5 | cooldown params, socket bug, conn_retry removal, num_retries=5 | 85.4%→100% |
| R12 | Removed proxy auto-compact; safety 120K→170K; contextWindow 120K→170K; InputReject→invalid_request_error | 80%→97% |
| R7 | CHARS_PER_TOKEN 2.0→3.0; safety 170K→190K; contextWindow 170K→190K; compactWindow 150K→180K | 99.6% |
| R15 | compactWindow 180K→140K (GLM IQ); contextWindow/safety 190K→170K (alignment) | 99.8% |
| R16 | compactWindow 140K→155K (CC overestimation 1.7x → too early compact) | 99.8% best ever |
| R17 | opc2_uname full sync: docker-compose.yml + litellm num_retries 30→8 + settings.json 155K + HTTPS_PROXY + proxy.py parity | 99.8%+ stable |
| R14 | Shell env vars fix (.bashrc+.profile+restart_claude.sh) | CC startup stable |

## 11 Immutable Variant Model IDs

**GLM-5.1 (41003/41001):** `zhipuai/glm-5.1`, `ZHipuAI/GlM-5.1`, `ZhIpuAI/GLm-5.1`, `ZhiPuAI/gLM-5.1`, `ZhipUAI/GlM-5.1`, `ZhipuAi/GLM-5.1`, `ZhipuaI/GLm-5.1`, `zhipuAI/gLM-5.1`, `ZHIPUAI/GLM-5.1`, `zhipuai/GLM-5.1`, `ZhiPUAI/glm-5.1`

**DSv4P (42001):** `deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`, `deepseek-ai/DeEpSeek-V4-Pro`

**NEVER modify/delete these — each variant has independent 200/id/day quota. rpm=1 per deployment is also immutable.**