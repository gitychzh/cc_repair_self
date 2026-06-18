# Round 272 — 2026-06-19 (基线轮)

## ⏱ 数据时间节点（防重复分析）
- ANALYZED_UNTIL: 2026-06-18T18:51:13Z   # 下轮从此之后截取，之前已分析
- 下轮命令: `docker logs auth_to_api_40005 --since "2026-06-18T18:51:13Z" -t 2>&1`

## 本轮基线数据（窗口 06-15T00:11:00Z → 06-18T18:51:13Z，约4天）
- [REQ] 总请求: 66 | 成功(KEY-CYCLE-SUCCESS): 19 | 成功率 ~29%
- ABORT-NO-FALLBACK(7key全429): 14 | ALL-KEYS-TRANSIENT: 14 | KEY-CYCLE: 171
- 502/500/timeout: 0 | variant-fallback: 0 | LiteLLM-fallback: 0
- rr_counter(glm5.2): 575（自重启处理~97 req）
- 14次ABORT集中在 18:16-18:42 burst窗口；burst恢复后(18:40+)仍有零散成功

## 关键发现（本轮新）
1. **transient ABORT retry-after=10，与CLAUDE.md参数表(=5)不一致** — 历史回归(R31.5拆分引入)。但10比5对burst更保守(CC等更久重试)，保留不改
2. burst非"所有key同时429"，而是"个别variant的7key陆续429" → KEY-CYCLE-SUCCESS是主要恢复路径(19/66成功都靠它)
3. burst窗口~15min，retry-after=10s让CC在窗口内重试~多次，形成14连击ABORT

## 本轮改动
- 无改动（burst是ModelScope RPM固有限制；throttle=2.0s已生效；retry-after方向需更多数据）

## 下轮待办（实验设计）
- 对比实验：若仍高频ABORT，可试 MIN_OUTBOUND_INTERVAL_S 2.0→2.5（更稀疏出站，可能降burst期429，但牺牲QPS；需测净收益）
- 或 retry-after 10→15（让CC重试更晚，给burst更多恢复）— 注意>60s CC会放弃报错

## 参数现状
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | COMPACT=155000 | THROTTLE=2.0s | transient-ABORT-retry-after=10
