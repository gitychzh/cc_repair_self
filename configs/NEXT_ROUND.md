# Round 240 — 2026-06-14 18:32

## 本轮数据
- R240(18:11→18:32): 11req(10m窗口) | 1×ALL-KEYS-429(v3全7key+fallback v4k7/v5k7) | 1×KEY-CYCLE-SUCCESS(v4k4) | 7×429-cycling | 0×502/500/timeout | 0×variant-fallback | 0×LiteLLM-fallback

## v×k第三轮v5k3，v3/v4短暂429 burst后恢复，正常ModelScope token quota暂时性耗尽

## 本轮改动
- 无改动（429 burst是暂时性，15分钟自动恢复，不可通过配置修复）

## 下轮待办
- 正常监控

## 参数现状
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
