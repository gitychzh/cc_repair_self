# Round 2 — 2026-06-13 02:32

## 本轮数据
- 全���: 总=476 | 成功=416(87%) | 429 cycling=166次 | ConnectionRefused=26(旧容器)
- 恢复后(02:29+): 19req | P50=10357ms | TTFB P50=7651ms | 4端点全部200✅
- 429 burst已恢复 | ms_rem avg=1945(充足)
- P99=79632ms(长上下文200+msgs,在300s PROXY_TIMEOUT范围内)
- R26 LiteLLM fallback: 0触发(新容器无ConnectionRefused事件)
- variant fallback: 2次全429触发(1次成功)

## 本轮改动
- 无改动。429已恢复，参数当前工作正常

## 下轮待办
- 继续监控429频率(恢复后应为偶发)
- 如果出现ConnectionRefused→验证R26 LiteLLM fallback是否真正生效
- P99=79s是否需要关注(长上下文请求本身慢，非配置问题)

## 参数现状
PROXY_TIMEOUT=300 | CPT=3.0 | SAFETY=170000 | COMPACT=155000
