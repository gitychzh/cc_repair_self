# Round R35.4 — 2026-06-21

## R35.4 数据分析总结（03:20-03:45 CST, throttle=1.5 纯MS）

### 40005 metrics after 1.5s deployment (03:20+, ~25 minutes)
- **162 requests, 100% status 200**
- **0 429s (final), 0 ABORT, 0 empty responses**
- **Upstream TTFB avg: 2979ms** (vs 3480ms at 2.0s) — 14% improvement
- **Zero cycling rate: 61.2%** (vs 56.8% at 2.0s)
- **No dispatcher fallbacks** in last 30 min (2 events during planned rebuild only)

### Before 03:20 (2.0s interval) reference
- 535 requests, 100% status 200
- 43 empty responses (est_tokens >117k, near context limit — CC auto-compact artifacts)
- Upstream TTFB avg: 3480ms
- Zero cycling rate: 56.8%

### Key finding: empty responses disappeared after 03:20
- Before: 43/535 = 8.0% empty responses
- After: 0/162 = 0% empty responses
- Root cause: empty responses had est_tokens >117k (near 170k context limit)
- Likely CC session context grew too large before auto-compact triggered
- Session restart/new context resolved this — NOT a proxy code change effect

### 40001 (baseline) comparison
- 39 requests today (low traffic via fallback only)
- 7 empty responses (17.9% — high due to old NV-enabled config before R35.2 rebuild)
- After 03:27 rebuild: 5 requests, 0 empty, TTFB 1143ms

### Dispatcher fallback events
- 35 total fallback events in dispatcher log (all from container rebuild periods)
- 7 "BOTH upstreams failed" (during 40005+40001 rebuild sequence)
- 0 fallback events in last 30 minutes — stable

## R35.4 Changes (stability-first, no risky parameter changes)

### 1. Log rotation — logger.py startup cleanup
- **Problem**: proxy logs grow ~1.4MB/day per proxy, no cleanup mechanism
- **Solution**: `_cleanup_old_logs()` runs on startup, deletes .log/.jsonl files older than LOG_RETENTION_DAYS
- **Env var**: LOG_RETENTION_DAYS=7 (all 4 proxy containers)
- **Safety**: only deletes dated log files, never touches rr_counter.json or config files
- **Files modified**: logger.py (cc-proxy, codex-proxy, passthrough-proxy), docker-compose.yml

### 2. Stale directory cleanup
- Removed empty dirs: litellm-nv-41006~41010, proxy-40002 (old NV LiteLLM instances from R33)
- /opt/cc-infra/logs/proxy/ left intact (stale dispatcher-era data, will be cleaned by script later)

### 3. External cleanup script
- `scripts/log_cleanup.sh`: crontab-ready, targets all proxy/litellm log dirs
- Not yet added to crontab (will do in next round or when user approves)

### What was NOT changed
- **MIN_OUTBOUND_INTERVAL_S remains 1.5** — no further reduction in unattended mode
- **NV_NUM_KEYS remains 0** — NV API still unavailable
- **All other parameters unchanged** — stability paramount during 8h sleep

## 累计优化效果

| 指标 | 原始(NV=5, throttle=2.0) | R35.1(NV=0, throttle=2.0) | R35.2+R35.4(throttle=1.5) |
|------|--------------------------|---------------------------|---------------------|
| avg upstream TTFB | ~60s(NV拖慢) | 3480ms | 2979ms (14% faster) |
| 429 cycling rate | N/A | 43.2% | 38.8% |
| success rate | 100% | 100% | 100% |
| empty output | 8.0% | 8.0% | 0% |
| ABORT-NO-FALLBACK | 0 | 0 | 0 |
| dispatcher fallback | 0 steady-state | 0 steady-state | 0 steady-state |
| log rotation | None | None | 7-day auto-cleanup |

## Round 5 待办
- 继续监控稳定性（无人值守8h+）
- 如果发现 429 突增或 fallback 事件 → 立即回退到 throttle=2.0
- 用户醒来后讨论：是否测试 throttle=1.0（需有人值守）
- NV API 恢复监测（可用 compare_proxies.sh 定期检查）
- 添加 log_cleanup.sh 到 crontab（`0 2 * * * /path/to/log_cleanup.sh`）
- 监控空响应是否再次出现（如出现，考虑调整 autoCompactWindow）

## 参数现状 (40001=40005 mirror)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7
