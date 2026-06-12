# Round 1 — 2026-06-13 02:30

## 本轮数据（30min窗口）
- 总请求=417 | 成功=362 | 错误=55(status=0=流未完成)
- 429 cycling=138次 | 涉及45个unique请求 | variant分布: v0=30 v1=18 v2=12 v3=4+v3=全429burst v4=7 v5=11 v6=12 v7=29 v8=6 v9=9
- 502=0 | 500=0 | timeout=0
- ConnectionRefused=26次(01:17时ms_uni41001重启) → 3次all_keys_connection_or_429 → 旧proxy无R26 fallback，新proxy已修复
- LiteLLM fallback=0触发(旧容器无此功能) | variant fallback=1次成功(v3 4keys→v3 k1)
- 延迟 P50=11244ms | P99=80885ms | max=136655ms
- TTFB P50=8404ms | max=43124ms
- ms_requests_remaining: min=1908 max=1999 avg=1951（quota充足）
- Agent: _cc=398 _ol=15 _cx=4
- 当前状态: 429 burst（全variant全key 429），预计15min恢复

## 本轮改动
- 无改动。数据正常，429是ModelScope quota burst(非配置问题)

## 下轮待办
- 等429恢复后重新测试，确认新proxy的R26 LiteLLM fallback是否真正生效
- 观察P99=80885ms是否偏高(当前PROXY_TIMEOUT=300s=300000ms，P99在范围内)
- 关注ConnectionRefused→LiteLLM fallback是否在新容器上正常工作

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
