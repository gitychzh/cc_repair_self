# Round 119 — 2026-06-13 22:02

## 本轮数据
- R119(21:52→22:01): ~3req/~2ok | 429 cycling: v7×12(7+5) | variant fallback: v8k3/v9k3→429→ALL-KEYS-429×1 | 0×502/500/timeout

## 429分析
- 第2次ALL-KEYS-429(R108=R1次)：v7全7key+v8/v9 fallback也429
- 跨variant token quota同时耗尽，非配置问题
- 22:01 v7恢复：5×429→k2成功

## 本轮改动
- 无改动

## 下轮待办
- 继续监控

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
