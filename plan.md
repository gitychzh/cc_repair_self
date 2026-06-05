# Plan: 修复 ModelScope "inappropriate content" 400 错误导致 CC 卡死

## 根本原因

ModelScope 内容审核返回 400 `BadRequestError: "Input data may contain inappropriate content"`。
当前 proxy 的 `_convert_error()` 将此错误映射为 `api_error` → CC 无限 retry 相同内容 → 永远被审核拦截 → **卡死**。

### 链路追踪

```
ModelScope 内容审核 → 400 BadRequestError "inappropriate content"
→ LiteLLM: BadRequestErrorAllowedFails=0, deployment 立即 cooldown 10s, 但 num_retries=5 换 deployment retry
→ Proxy: _convert_error() → 不匹配 is_input_overflow, 不匹配 thinking_budget → 默认 api_error
→ CC: api_error → retry 相同内容 → 再次被审核拦截 → 无限循环 → 卡死
```

### 核心矛盾

"inappropriate content" 不像其他 400 错误：
- 不是参数错误（retry 同内容永远失败）
- 不是 token 超限（不能靠 compact 解决）
- 不是限速（不会自动恢复）
- CC 对 api_error 会无限 retry → **永远卡死**

## 解决方案：将 "inappropriate content" 映射为 `invalid_request_error`

- **CC 行为**：`invalid_request_error` → CC 立即停止，不 retry，不卡死
- **理由**：内容审核是 ModelScope 的不可恢复错误，retry 同内容永远不会通过审核。
  映射为 `invalid_request_error` 让 CC 立即知道这个请求无法完成，停止重试。
- **优点**：CC 不卡死，不浪费 quota（不 retry 5 次 × 无效请求）
- **缺点**：CC 会停止当前任务，但这比卡死好得多

对比其他方案：
- 映射为 overloaded_error → CC auto-compact → compact 后内容可能仍触发审核 → 反复循环 → 危险
- 映射为 rate_limit_error → CC backoff retry → retry 同内容永远失败 → 卡死

## 实施步骤

### Step 1: 修改 proxy.py `_convert_error()`

在 `_convert_error()` 中添加 "inappropriate content" → `invalid_request_error` 映射：

```python
# "inappropriate content" (ModelScope content safety filter) → invalid_request_error
# ModelScope content audit rejects input as inappropriate. This is NOT recoverable:
# retrying the same content will always fail (content audit is deterministic).
# Mapping to invalid_request_error lets CC stop immediately instead of infinite retry → freeze.
elif "inappropriate content" in msg_lower:
    err_type = "invalid_request_error"
```

位置：在 `is_quota_exhausted` 和 `rate` 检查之后，`range of input length` 检查之前。

### Step 2: 确保 is_input_overflow 不会误匹配 "inappropriate"

当前 `is_input_overflow` 检查不含 "inappropriate"关键词，无需修改。

### Step 3: 修复 proxy 容器路由指向（R1遗留）

当前 proxy 容器内环境变量仍指向 `glm5.1_test41003:4000`，
但 docker-compose.yml 已改为 `glm5.1_uni41001:4000`。
需要重建 proxy 容器使配置生效：
```bash
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001
```

### Step 4: 部署并测试

1. 备份配置
2. 同步 proxy.py 到 opc_uname /opt/cc-infra/proxy/
3. 重建 proxy 容器（应用新 error mapping + 正确路由）
4. 测试正常请求 200 OK
5. 测试错误场景（发送含审核触发词的请求验证返回 invalid_request_error）

## 修复后 CC 的行为变化

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| "inappropriate content" 400 | CC api_error → 无限retry → 卡死 | CC invalid_request_error → 立即停止 → 用户可继续 |
| "Range of input length" 400 | CC overloaded → auto-compact ✅ | 不变 ✅ |
| thinking_budget 400 | proxy resilience retry → ✅ | 不变 ✅ |
| 429 quota exhausted | CC rate_limit → backoff ✅ | 不变 ✅ |

## 风险评估

- 修改 `_convert_error()` 添加一个 `elif` 分支，风险低
- 映射为 `invalid_request_error`：CC 会停止，但比卡死好得多
- proxy 路由指向修正：R1 遗留问题，必须修复
- dsv4p 也使用 ModelScope API，同样受益于此修改