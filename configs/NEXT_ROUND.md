# Round 11 — 2026-06-13 04:02

## 本轮数据
- R11(03:52+): 8req/6ok | 429=8次cycling→1次cycling成功 | 502/500/timeout=0
- 延迟P50=14763ms | TTFB P50=13629ms | ms_rem avg=1883
- R10 burst余波持续，429频率下降(15→8) | 正在恢复

## 本轮改动
- 无改动。429余波持续恢复中，非配置问题

## 下轮待办
- 确认429完全恢复(频率应回到≤3/10min)
- R26待自然ConnectionRefused验证

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
