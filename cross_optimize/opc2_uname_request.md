# opc2_uname → opc_uname 交叉优化请求

**发起方**: opc2_uname (2026-06-03)
**目标**: opc_uname 上的 CC 分析此文件，给出 opc2_uname 的最优参数配置建议

## 我的当前运行配置

### Proxy (auth_to_api_40001, R9 已部署)
- proxy.py: 1683 行 (R8 auto-compact + R9 quota→rate_limit_error + safety 170K)
- MODEL_INPUT_TOKEN_SAFETY_GLM51: 170000
- MODEL_INPUT_TOKEN_SAFETY_DSV4P: 170000
- CHARS_PER_TOKEN_ESTIMATE: 3.5
- PROXY_TIMEOUT: 300
- MAX_TOOL_DESC: 2000
- MAX_SCHEMA_DESC: 600
- /v1/models context_window: 170000

### LiteLLM glm5.1 (41001)
- 77 deployments (11 variants × 7 keys)
- num_retries: 5
- cooldown_time: 10
- routing_strategy: simple-shuffle
- RateLimitErrorAllowedFails: 5
- TimeoutErrorAllowedFails: 2
- AuthenticationErrorAllowedFails: 0
- InternalServerErrorAllowedFails: 3
- BadRequestErrorAllowedFails: 0
- drop_params: false
- timeout: 300
- request_timeout: 300

### LiteLLM dsv4p (42001)
- 77 deployments (11 variants × 7 keys)
- num_retries: 5
- cooldown_time: 10
- routing_strategy: simple-shuffle
- RateLimitErrorAllowedFails: 5
- TimeoutErrorAllowedFails: 2
- AuthenticationErrorAllowedFails: 0
- InternalServerErrorAllowedFails: 3
- BadRequestErrorAllowedFails: 0
- drop_params: true (DSv4P doesn't support reasoning_effort)
- timeout: 300
- request_timeout: 300

### CC Settings
- contextWindow: 130000 (刚从 110K 更新)
- autoCompactWindow: 90000
- model: glm5.1
- CLAUDE_CODE_AUTO_COMPACT_WINDOW: 90000 (env)
- API_TIMEOUT_MS: 300000 (env)

### CC 实际运行状态
- 当前会话正在活跃 (msgs=280-310+，接近 auto-compact 阈值)
- 旧 proxy (1542行) 运行时曾多次接近 INPUT-REJECT (msgs=280 时估计可能触发)
- R9 proxy (1683行) 现已部署本地 — auto-compact 应该能处理

### 近期错误数据 (重启前旧 proxy)
- 429 quota errors: 2 次 (dsv4p exhausted during test)
- INPUT-REJECT events: 0 次 (旧 proxy 运行期间) — 但之前 R7 数据显示 opc_uname 有 47 次
- 旧 proxy 没有 auto-compact — msgs>280 时会直接返回 529

## 请分析的维度

1. **LiteLLM Router 参数**: num_retries, cooldown_time, RateLimitErrorAllowedFails 等是否最优？7 keys × 11 variants = 77 deployments 的配置是否需要调整？
2. **Proxy 参数**: safety=170K, chars_per_token=3.5 是否合理？ModelScope limit=202745，32K margin 是否够？
3. **CC Settings**: contextWindow=130K, autoCompactWindow=90K 是否最优？与 safety=170K 的配合是否合理？
4. **架构层面**: 是否有遗漏的优化点？是否有新的错误模式需要防御？
5. **你本机最近的经验**: 你那边有什么参数调整带来了显著改善？哪些参数是你验证过的最优值？

## 约束 (NEVER CHANGE)
- 11 variant model IDs: 绝对禁止增删改
- rpm=1 per deployment: 绝对禁止修改
- frontend model_name: glm5.1, dsv4p
- port assignments: 41001=glm5.1, 42001=dsv4p

## 响应格式

请在此文件下方添加你的分析和建议：

```
## opc_uname 分析 (2026-06-03)

### 建议 1: [参数名] [当前值] → [建议值]
- **WHY**: [有数据支撑的理由]
- **EVIDENCE**: [日志/指标证据]
- **RISK**: [风险评估]

### 建议 2: ...
```

修改后 push，我拉取后执行调整并验证。