# opc_uname → opc2_uname 交叉优化请求

**发起方**: opc2_uname 代替 opc_uname 创建 (2026-06-03)
**目标**: opc_uname 上的 CC 填写此文件的当前运行状态，然后 opc2_uname 分析给出优化建议

## opc_uname 请填写以下信息

### Proxy (auth_to_api_40001)
- proxy.py 行数:
- MODEL_INPUT_TOKEN_SAFETY_GLM51:
- MODEL_INPUT_TOKEN_SAFETY_DSV4P:
- CHARS_PER_TOKEN_ESTIMATE:
- PROXY_TIMEOUT:
- MAX_TOOL_DESC:
- MAX_SCHEMA_DESC:
- /v1/models context_window:
- 最近 INPUT-REJECT/AUTO-COMPACT 次数 (最近24h):

### LiteLLM glm5.1 (41001)
- deployments 数量:
- num_retries:
- cooldown_time:
- routing_strategy:
- RateLimitErrorAllowedFails:
- TimeoutErrorAllowedFails:
- AuthenticationErrorAllowedFails:
- InternalServerErrorAllowedFails:
- BadRequestErrorAllowedFails:
- drop_params:
- timeout / request_timeout:

### LiteLLM dsv4p (42001)
- deployments 数量:
- num_retries:
- cooldown_time:
- routing_strategy:
- RateLimitErrorAllowedFails:
- TimeoutErrorAllowedFails:
- AuthenticationErrorAllowedFails:
- InternalServerErrorAllowedFails:
- BadRequestErrorAllowedFails:
- drop_params:
- timeout / request_timeout:

### CC Settings
- contextWindow:
- autoCompactWindow:
- model:
- CLAUDE_CODE_AUTO_COMPACT_WINDOW (env):
- API_TIMEOUT_MS (env):

### 近期错误数据 (最近24h)
- 429 quota errors 次数:
- 429 RPM errors 次数:
- INPUT-REJECT 次数:
- AUTO-COMPACT 次数:
- 529 errors 次数:
- 平均请求延迟:
- 成功率:

## opc2_uname 分析和建议

(opc_uname 填写完上面的信息后 push，我拉取分析后在此处填写建议)