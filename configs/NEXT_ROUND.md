# Round 147 — 2026-06-14 02:52

## 本轮数据
- R147(02:41→02:51): ~3req/~2ok | 429 cycling: v2×7 | variant fallback: v3k2/v4k2→429→ALL-KEYS-429×1(第3次) | 0×502/500/timeout | _ol×1触发burst

## 第3次ALL-KEYS-429
- R108=1st(v7+v8/v9), R119=2nd(v7+v8/v9), R147=3rd(v2+v3/v4)
- 跨variant token quota同时耗尽，~8min后恢复(v2k3/k4直通)

## 本轮改动
- 无改动

## 下轮待办
- 继续监控

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
