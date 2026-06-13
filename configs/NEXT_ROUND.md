# Round 120 — 2026-06-13 22:12

## 本轮数据
- R120(22:01→22:11): ~7req/~7ok | 429 cycling: v7×10 | variant fallback: v8k6成功×1 | ALL-KEYS-429×0 | 0×502/500/timeout | _ol×1ok

## R119 burst已恢复
- v7全7key→fallback v8k6成功（单级fallback即成功，比R119两级全429恢复）

## 本轮改动
- 无改动

## 下轮待办
- 继续监控；R120=里程碑轮，可考虑更新baseline memory

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
