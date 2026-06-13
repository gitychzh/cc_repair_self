# Round 118 — 2026-06-13 21:52

## 本轮数据
- R118(21:41→21:51): ~7req/~7ok | 429 cycling: v6×10 | variant fallback: v7k4→429→v8k4成功×1 | ALL-KEYS-429×0 | 0×502/500/timeout | _ol×2ok

## 两级fallback连续验证
- R116: v4→v5k4(429)→v6k4(成功)
- R118: v6→v7k4(429)→v8k4(成功) — 机制可靠

## 本轮改动
- 无改动

## 下轮待办
- 继续监控

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
