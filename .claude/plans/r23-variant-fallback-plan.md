# R23: Proxy ALL-KEYS-429 预防性改进

## 问题回顾 (2026-06-12 事件)

- 5 个 CC 进程同时消耗 ModelScope token quota → glm5.1 + dsv4p 7个key全 429
- proxy 同 variant 7 key cycling → 全429 → 返回 `rate_limit_error(retry-after=30s)`
- CC 收到 rate_limit_error → backoff 30s → retry → 又7×429 → 每次重试浪费7个quota → 恶性循环
- 从 17:00 到 17:40 期间，23次 ALL-KEYS-429，每次消耗 7 个 key quota
- 总浪费: 23 × 7 = 161 次 quota 消耗（仅返回错误，没有任何有用输出）

## 改进方案

### 改动 1: 跨 variant fallback (核心改动)

**当前逻辑**: 同 variant 7 key 全429 → 直接返回 rate_limit_error
**新逻辑**: 同 variant 7 key 全429 → 尝试其他 variant（最多 2 个额外 variant），每个新 variant 只尝试 1 个 key

**为什么跨 variant 有效**：
- 虽然 token quota 是 per-key（跨 variant 共享），但数据表明不同 variant 的 ALL-KEYS-429 不是同时发生
- 说明 token quota 耗尽有先后顺序——某个 key 可能在此 variant 刚耗尽但在另一个 variant 还有余量
- 这可能是因为 ModelScope 的 token quota 有 per-model-id 的粒度（每个 variant model ID 有独立额度），或者恢复时间窗口不同

**实际效果**：10 variant × 7 key = 70 个 deployment。当前只用 7 个就放弃，利用率仅 10%。跨 variant 可以尝试最多 3 个 variant = 21 个 deployment，利用率 30%。

**重要限制**：
- 每个 fallback variant 只尝试 1 个 key（而不是 7 个），避免大量 quota 浪费
- 如果 1 个 key 也 429，立即跳到下一个 variant
- 最多尝试 2 个额外 variant（start_variant → +1 → +2），超过后返回 429
- 这样每次请求最多尝试 7 + 2 = 9 个 key（而非 7 + 14 = 21 个），大幅减少 quota 浪费

### 改动 2: 增加 Retry-After 时间

**当前**: `retry-after: 30` (30秒)
**问题**: CC 每30秒重试一次，每次消耗7个quota，15分钟内浪费161个quota
**新值**: `retry-after: 180` (3分钟)

**原因**: ModelScope token quota 恢复时间约15分钟。180s retry-after 让 CC 重试间隔更长，减少恶性循环中的 quota 浪费。180s 也足够让 quota 有部分恢复（不是完全恢复，但可能有几个key恢复）。

### 改动 3: 识别 429 类型优化 cycling 策略

**当前**: 所有 429 都 cycling 7 key（不管错误类型）
**问题**: "token-limit" 429 表示所有 variant 共享的 per-key quota 耗尽，换 key 无用
**新逻辑**: 第一个 429 返回后，检查 error body 中的 429 类型：
- 如果是 **token quota** 错误（"token-limit", "check your plan and billing details"）→ 仍然 cycling（因为可能只是这一个key耗尽，其他key还有余量）
- 如果是 **RPM quota** 错误（"rate limit", "requests per minute"）→ cycling 更有效（RPM per variant）

（实际上当前的所有 429 都是 token quota 错误，cycling 对 token quota 的效果有限——但我们无法在收到第一个 429 时就知道所有 7 个 key 都耗尽，所以 cycling 仍然是必要的）

## 代码改动细节

### proxy.py 改动

**位置**: `do_POST` 方法中，key cycling loop (line 832-1107) 和 ALL-KEYS-429 处理 (line 1171-1258)

**改动结构**:
```python
# 当前: 单层 loop (7 key in same variant)
for attempt_idx in range(NUM_KEYS):
    current_key_idx = (start_key_idx + attempt_idx) % NUM_KEYS
    ...

# 新增: 在 key cycling loop 完成后，如果全429，进入 variant fallback
# 在 line 1171 之前，新增 variant fallback 逻辑
if all_429:
    # Try up to 2 additional variants
    num_variants = NUM_VARIANTS.get(mapped_model, 10)
    variant_fallback_attempts = []
    for fallback_v_offset in range(1, min(3, num_variants)):  # try v+1, v+2
        fallback_v_idx = (start_variant_idx + fallback_v_offset) % num_variants
        fallback_k_idx = start_key_idx  # use the same starting key
        litellm_model = f"{litellm_model_base}v{fallback_v_idx+1}k{fallback_k_idx+1}"
        
        # Try just 1 key in this fallback variant
        try:
            conn = self._make_upstream_conn(parsed_upstream)
            conn.request(...)
            resp = conn.getresponse()
            if resp.status < 400:
                # Success! Record variant fallback success
                metrics["variant_fallback"] = True
                metrics["fallback_variant_idx"] = fallback_v_idx
                metrics["fallback_key_idx"] = fallback_k_idx
                # ... handle success (stream/collect/etc)
                return
            elif resp.status == 429:
                variant_fallback_attempts.append(...)
                _log("VARIANT-FALLBACK-429", ...)
                conn.close()
                continue  # try next fallback variant
            else:
                # Non-429 error in fallback — break and report
                break
        except socket.timeout:
            variant_fallback_attempts.append(...)
            continue
        except Exception:
            continue
    
    # All fallback variants also failed → fall through to existing ALL-KEYS-429 handling
    # but with increased retry-after
```

**关键设计要点**:
1. 每个 fallback variant 只尝试 **1 个 key**（不是 7 个），最小化 quota 浪费
2. 最多 2 个额外 variant（start_variant → next → next_next），避免过度消耗
3. Fallback 成功时记录 `variant_fallback=True` 和 variant/key 信息到 metrics
4. Fallback 失败时也记录到 error_detail 日志
5. `retry-after` 从 30s 改为 180s

**metrics 新增字段**:
- `variant_fallback`: bool — 是否通过 variant fallback 成功
- `fallback_variant_idx`: int — 成功的 fallback variant
- `fallback_key_idx`: int — 成功的 fallback key
- `variant_fallback_attempts`: list — 所有 fallback 尝试记录

**error_detail 新增字段**:
- `variant_fallback_attempts`: list — 所有 fallback 尝试记录
- `variant_fallback_all_failed`: bool — 所有 fallback variant 也 429

**日志新增标签**:
- `VARIANT-FALLBACK-TRY`: 尝试 fallback variant
- `VARIANT-FALLBACK-SUCCESS`: fallback variant 成功
- `VARIANT-FALLBACK-429`: fallback variant 也 429

## 风险评估

1. **Quota 消耗增加**: 每次请求从最多 7 个 key 尝试增加到最多 9 个（7+2），增加约 29%。但相比不 fallback 时 CC 反复重试浪费的 161 个 quota，这是小量
2. **延迟增加**: 每个额外的 fallback key 尝试约 0.5-1s（429 响应很快），最多增加 2s
3. **Token quota 确实跨 variant 共享**: 如果所有 7 个 key 的 token quota 确实同时耗尽（所有 variant），fallback 2 个额外 variant 也只是浪费 2 个 quota。但风险可控（只浪费 2 个，而非 14 个）
4. **RPM quota per variant**: 如果是 RPM 429（而非 token quota），fallback variant 有独立 RPM quota，可能成功

## 不改的部分（CLAUDE.md 约束）

- 所有 variant model IDs 不变
- rpm=1 per deployment 不变
- LiteLLM config 不变（num_retries=0, all allowed_fails=0）
- frontend model names 不变
- Container names 不变
- Port assignments 不变

## 部署步骤

1. 备份: `bash scripts/backup_config.sh`
2. 修改 proxy.py: 加入 variant fallback + retry-after=180
3. 重建 proxy 容器: `cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002`
4. 测试: curl 验证 glm5.1 和 dsv4p 返回 200
5. 更新 DEPLOY_STATUS.md
6. Push 到 GitHub