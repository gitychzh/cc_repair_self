# Round 150 — 2026-06-14 03:22 (里程碑)

## 本轮数据
- R150(03:11→03:21): ~7req/~7ok | 429=0 | 0×502/500/timeout | v4→v5直通 | _ol×2ok

## R150里程碑：R107→R150共43轮(约6h) 0配置变更
- 3×ALL-KEYS-429(R108/R119/R147) 均为ModelScope token quota burst
- 4×两级fallback成功(R116/R118/R129/R130)
- 0×502/500/timeout, 0×LiteLLM fallback触发
- v×k完成3轮完整轮转(v1→v10→v1→v10→v1)

## 本轮改动
- 无改动

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
