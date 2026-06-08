# Plan: 删除 Proxy 压缩逻辑，简化为纯格式转换

## 问题
当前 auto-compact 有三条路径触发，每条都导致"彻底忘记上下文"：
1. **CC 内置 auto-compact**（settings.json `autoCompactWindow=90K`）：CC 自己压缩对话→摘要质量差→忘上下文
2. **Proxy 截断**（proxy.py `_auto_compact_messages`）：proxy 暴力砍掉早期消息→直接丢失→更粗暴
3. **400→529 overloaded 转换**（proxy.py）：ModelScope input overflow → proxy 转 529 → CC 触发 compact → 又忘上下文

三层防线互相矛盾、互相叠加，复杂且效果差。

## 方案
**删除所有 proxy 层面的压缩干预，让压缩完全由 CC 内置机制控制。**

### 1. proxy.py — 删除 auto-compact 截断逻辑

**删除** 整个 `if estimated_tokens > model_safety:` 块（lines 700-728）：
- 不再截断消息历史
- 不再返回 429 rate_limit_error (INPUT-REJECT-UNCOMPACTABLE)
- 保留 `INPUT-WARN` 日志告警（让运维知道对话变大了）
- 保留 `estimated_tokens` metrics 记录（数据分析仍有价值）

**删除** `_auto_compact_messages()` 方法（lines 1573-1681）：
- 整个方法不再需要

**修改** 400 input overflow 处理（lines 990-1007）：
- 不再转成 529 overloaded_error
- 改成转成 `invalid_request_error`（CC 直接停止，不压缩不重试）
- 用户看到错误信息就知道对话太长了，手动开新对话

**修改** `_convert_error()` 中 "Range of input length" 映射（line 1765-1767）：
- 从 `overloaded_error` 改为 `invalid_request_error`
- CC 收到 invalid_request_error 会停止，不会触发压缩

**删除** 529 overloaded → force overloaded_error（lines 1028-1038）：
- 不需要了，因为不再希望 CC 触发 compact

**保留不变**：
- `_estimate_text_chars()` 函数（metrics 需要）
- `estimated_tokens` metrics 记录（数据分析）
- `MODEL_INPUT_TOKEN_SAFETY` env 变量（仍用于 context_window 报告）
- `_anthropic_models_list()` 报告 context_window（仍需要，让 CC 知道何时触发内置 compact）

### 2. settings.json — 提高 autoCompactWindow

把 `autoCompactWindow` 从 90K 提到 110K，`contextWindow` 保持 120K：
- 90K → 110K：CC 在对话更长时才压缩，减少触发频率
- CC 内置压缩虽然也会丢上下文，但至少是 CC 自己做的摘要，比 proxy 暴力截断好
- 110K/120K 只有 10K buffer，CC 压缩时会更激进，但触发次数更少

### 3. CLAUDE.md — 更新可调整参数表

- 删除 proxy 层面 auto-compact 相关参数（已不存在）
- 更新 contextWindow/autoCompactWindow 当前值
- 更新"反思教训"章节

### 4. docker-compose.yml — 无变化

MODEL_INPUT_TOKEN_SAFETY env 仍然需要（用于 context_window 报告给 CC），但不再用于截断判断。

## 预期效果

- **简化**：proxy 只做格式转换 + metrics，不再有截断/压缩逻辑
- **减少"忘记上下文"频率**：proxy 不会暴力截断，CC 只在接近 120K 时才内置压缩
- **长对话保护**：如果真的超过 ModelScope 202745 上限，CC 收到 invalid_request_error 直接停止（不压缩不重试），用户看到错误手动开新对话
- **可能风险**：如果 CC 内置 compact 也丢上下文（你说确实如此），那只是比现在好一点（触发频率更低）。最终解决方案可能是定期手动开新对话，而不是依赖任何自动压缩

## 不变项

- 11 variant model IDs（绝对禁止修改）
- rpm=1 per deployment
- LiteLLM retry/fallback/routing 机制
- proxy force-stream 逻辑
- proxy tool description truncation 逻辑
- proxy error format 转换（429→rate_limit_error, inappropriate→invalid_request_error 等）