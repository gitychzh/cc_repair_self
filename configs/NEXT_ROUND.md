# Round 116 — 2026-06-13 21:32

## 本轮数据
- R116(21:21→21:31): ~5req/~5ok | 429 cycling: v4×7 | variant fallback: v5k4→429→v6k4成功×1 | ALL-KEYS-429×0 | 0×502/500/timeout

## 429分析
- v4全7key 429 → fallback v5k4也429 → fallback v6k4成功（两级fallback有效）
- burst后v4/v5恢复正常直通

## 本轮改动
- 无改动

## 下轮待办
- 继续监控

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
