# Round 190 — 2026-06-14 10:03

## 本轮数据
- R190(09:53→10:03): 4req/4ok | 2×ALL-KEYS-429(v2全7key) | 1×两级fallback(v2→v3 429→v4k2 ok) | 1×直接fallback(v2→v3k4 ok) | 1×cycling(v2k7) | 0×502/500/timeout

## v2跨variant token quota密集耗尽

## 本轮改动
- 无改动(暂时性429 burst，自动恢复)

## 下轮待办
- 监控v2恢复+v3轮换

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
