# Round 108 — 2026-06-13 20:12

## 本轮数据
- R108(20:01→20:11): ~8req/~7ok | 429 cycling: v6×1+v7×7 | ALL-KEYS-429×1(v7全key+fallback v8/v9) | 0×502/500/timeout | P50≈12s | _ol×2ok

## 429分析
- v7 token quota burst：全7key 429 + fallback v8/v9也429 → 正确触发all_keys_exhausted
- R79同样模式：ModelScope暂时性，15min自动恢复，非配置问题

## 本轮改动
- 无改动

## 下轮待办
- 继续监控，注意429 burst恢复情况

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
