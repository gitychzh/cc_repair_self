# Round 247 — 2026-06-14 19:52

## 本轮数据
- R247(19:41→19:52): 25req(高活跃) | 1×ALL-KEYS-429(v3全7key+fallback v4k4/v5k4) | 6×KEY-CYCLE-SUCCESS(v3k7,v3k6,v3k2,v4k3×2) | 23×429-cycling | 0×502/500/timeout | 0×variant-fallback | 0×LiteLLM-fallback

## v×k第五轮v4k7，v3/v4 429 burst但key cycling正常恢复

## 本轮改动
- 无改动（429 burst是暂时性token quota耗尽，不可配置修复）

## 下轮待办
- 正常监控

## 参数现状
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
