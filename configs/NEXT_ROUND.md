# Round 180 — 2026-06-14 08:23

## 本轮数据
- R180(08:13→08:23): 7req/7ok | 1×ALL-KEYS-429(v3+v4+v5全429) | 1×KEY-CYCLE-SUCCESS(v2k7,3-key) | 0×502/500/timeout

## ALL-KEYS-429详情: v3全7key 429→fallback v4k1+v5k1也429→跨variant token quota耗尽

## 本轮改动
- 无改动(429 burst是ModelScope暂时性，自动恢复)

## 下轮待办
- 继续监控v3恢复

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
