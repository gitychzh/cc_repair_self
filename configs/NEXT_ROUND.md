1	# Round R35.15 (第8轮, 最终轮) — 2026-06-22 19:30 CST

2	
3	## R35.15 验证数据（opc2_uname, 4h+24h 运行数据）
4	
5	### 40005 (cc-proxy, EXPERIMENT) — 765 requests in 24h
6	
7	| 指标 | R35.15 (4h) | R35.15 (24h) | R35.14 (opc_uname 24h) |
8	|------|-------------|--------------|------------------------|
9	| 总请求 | 282+478 | 765 | 1272 |
10	| ABORT率 | 0.9% (7/760) | 0.9% (7/765) | 0% (opc_uname) |
11	| KEY-CYCLE-SUCCESS | 282 | ~280 | 0 |
12	| 429 cycling | 正常 RPM burst | 正常 | 正常 |
13	| NV traffic | 0 (NV disabled) | 0 | 0 |
14	
15	### 40001 (MIRROR/STABLE) — 33 requests (少量直连)
16	### 40002 (codex) — 极低流量
17	### 40003 (passthrough) — 33 requests, 0 ABORT, 8 KEY-CYCLE-SUCCESS
18	
19	### ModelScope DNS — 0 DNS errors in 24h ✅
20	（R35.14的35min DNS outage未复现，是瞬态事件）
21	
22	### NV API 测试 — 3/3 200 ✅
23	
24	| 测试 | HTTP | 延迟 |
25	|------|------|------|
26	| test 1 | 200 | ~1s (首测) |
27	| test 2 | 200 | 10.8s |
28	| test 3 | 200 | 9.8s |
29	
30	NV API 连续2轮可用（R35.14+R35.15），但延迟偏高（9-11s vs opc_uname R35.14的1.1-7.4s）。
31	opc2_uname mihomo 7894 已有配置，但US-NV节点选择策略可能需要调优。
32	
33	### 所有容器 — ✅ healthy (7/7)
34	
35	| 容器 | 状态 | 运行时间 |
36	|------|------|----------|
37	| auth_to_api_40000 | healthy | 4h |
38	| auth_to_api_40001 | healthy | 4h |
39	| auth_to_api_40002 | healthy | 9h |
40	| auth_to_api_40003 | healthy | 4h |
41	| auth_to_api_40005 | healthy | 4h |
42	| cc_postgres | healthy | 9h |
43	| ms_uni41001 | healthy | 9h |
44	
45	---
46	
47	## Action: 本轮无需修改 ✅ → 项目完工！
48	
49	**连续5轮无需修改计数: 5/5 ✅ PROJECT COMPLETE**
50	
51	- R35.11: SSE buffer fix确认 → 无修改
52	- R35.12: NV API再次不可用 → 无修改  
53	- R35.13: NV 429 rate-limited → 无修改
54	- R35.14: NV恢复+DNS outage自恢复 → 无修改
55	- R35.15: 系统全面稳定+NV连续2轮可用 → 无修改
56	
57	**不做的事**（稳定优先原则始终生效）:
58	- ❌ 不重新启用 NV interleaving: API可用但延迟不稳定(9-11s)，NV仍是optional增强而非核心依赖
59	- ❌ 不降低 throttle: 1.5s间隔是429 burst的核心缓解
60	- ❌ 不修改任何参数: 系统稳定
61	
62	**NV re-enable 遗留项**（如未来需要，按以下步骤操作）:
63	1. opc2_uname mihomo调优7894 US-NV节点选择（当前延迟9-11s，需降到2-5s）
64	2. NV连续多轮（≥3轮）可用确认
65	3. docker-compose.yml NV_NUM_KEYS=2 (小范围测试)
66	4. rebuild proxy容器 + 功能测试
67	5. 逐步提升到NV_NUM_KEYS=5
68	
69	---
70	
71	## 项目完工总结 — R35 自优化系统
72	
73	### 核心成就
74	
75	1. **R35 Dispatcher + 自动 fallback**: 40000端口按model字段路由（opus→40005, sonnet→40001），连接失败自动切换
76	2. **蓝绿 CC proxy**: 40005(experiment)+40001(stable)，新参数先在40005测试
77	3. **SSE buffer-based parsing**: 修复finish_reason=None（7.2%→87.5%→100% FR）
78	4. **MSG-FIX**: passthrough自动追加user "Continue."修复assistant-ending序列
79	5. **throttle 对齐**: 全端口1.5s MIN_OUTBOUND_INTERVAL（数据驱动，2.0→1.5不增429）
80	6. **NV glm-5.1 禁用**: API不可用时0%成功→全端口纯MS，避免请求浪费
81	7. **ABORT-NO-FALLBACK**: 7key全429→终止返回错误，防止17x放大消耗
82	8. **Variant×Key 2D round-robin**: 10 variants×7 keys = 70 dep，行优先counter持久化
83	9. **log rotation**: 7天自动清理，避免磁盘满
84	10. **R31.8/R31.9 出站节流**: MIN_OUTBOUND_INTERVAL=1.5s缓解ModelScope RPM burst throttle
85	11. **CC auto-compact 理解**: tokenizer高估1.7x，autoCompactWindow需考虑偏差
86	12. **529→CC崩溃路径**: 绝不转换429→529
87	
88	### 从灾难到稳定的关键修复
89	
90	| 问题 | 修复 | 轮次 |
91	|------|------|------|
92	| SSE finish_reason=None 7.2% | buffer-based parsing | R35.9 |
93	| OpenClaw "Cannot continue from assistant" | MSG-FIX (auto-append user) | R35.10 |
94	| NV API unavailable 0% success | NV disabled (pure MS) | R35.3 |
95	| 429→17x variant fallback放大 | ABORT-NO-FALLBACK | R31.8 |
96	| proxy retry +37%延迟 | proxy不retry, CC自己重试 | R29 |
97	| proxy auto-compact上下文丢失 | 绝不做截断/压缩 | R28 |
98	| 429→529→CC崩溃 | 绝不转换状态码 | R28 |
99	| ModuleNotFoundError→proxy崩溃 | 改import后确认.py存在 | R27 |
100	| stale deploy (code≠container) | sync+rebuild+smoke test | R35.7 |
101	| throttle 2.0→1.5 数据验证 | 跨proxy RPM竞争不增429 | R35.8 |
102	
103	### 系统稳定性指标（R35.15 最终状态）
104	
105	- **成功率**: 99.1% (40005, 429 cycling正常恢复)
106	- **ABORT率**: 0.9% (760 req中7 ABORT，RPM burst正常范围)
107	- **DNS resilience**: ModelScope DNS outage自恢复，proxy无法防护但CC重试透明覆盖
108	- **NV API**: 连续2轮可用但延迟不稳定，保持disabled
109	- **所有7容器**: healthy，无异常
110	
111	### 参数现状 (R35.15 FINAL)
112	
113	PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False | dispatcher auto-fallback | SSE buffer-based parsing (R35.9 STABLE) | MSG-FIX (R35.10 VERIFIED) | ABORT-NO-FALLBACK (R31.8 STABLE)
114	
115	---
116	
117	## 连续无需修改计数: 5/5 ✅ → PROJECT COMPLETE
118	
119	**自优化循环已完成使命。系统稳定运行，无需进一步调整。**
120	
121	### 未来可选优化（非必需，风险需评估）
122	
123	1. **NV interleaving 重启用**: 需NV API持续可用+延迟稳定(≤5s)+7894节点调优
124	2. **MS RPM 提升**: 等 ModelScope 商业方案（当前 RPM=1 是硬限制）
125	3. **更智能的 429 预测**: 基于 time-of-day + recent-burst-history 的动态 throttle
126	4. **跨机器 metrics dashboard**: Prometheus + Grafana 可视化
127	
128	以上均不影响当前系统稳定性，按需评估后实施。
