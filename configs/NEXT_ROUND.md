# Round 109 — 2026-06-13 20:22

## 本轮数据
- R109(20:11→20:21): ~10req/~10ok | 429 cycling: v8×2+v9×6=8 | ALL-KEYS-429×0 | 0×502/500/timeout | _ol×2ok

## 429分析
- R108 v7 burst已恢复：v8/v9有部分key 429但都有可用key，cycling成功
- 无ALL-KEYS-429，burst缓解

## 本轮改动
- 无改动

## 下轮待办
- 继续监控

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
