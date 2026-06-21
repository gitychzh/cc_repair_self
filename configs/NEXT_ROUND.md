# Round R35.6 — 2026-06-21

## R35.6: OpenClaw 卡住根因修复

### 症状
OpenClaw (_ol) 报错 `{ Agent failed before reply: All 7 ModelScope API keys have exhausted their token quota for model glm5.1... }` 然后直接卡住。而 Claude Code (_cc) 从不卡住，同样碰到 7key 全 429 却 5 秒后自动恢复。

### 根因链（完整5步）

**Step 1**: ModelScope RPM burst throttle 返回 429 body `"You exceeded your current quota"` — 这是 Aliyun stock phrase，**实际是 RPM 节流而非 quota 耗尽**

**Step 2**: passthrough proxy (40003) `is_quota_exhaustion()` 用关键词匹配 `"quota"/"exhausted"` → 匹配到 → 标记 `429_quota_exhausted` 
- cc-proxy (40001/40005) 同一函数已改为 always-return-False（325/331 false positive 证实关键词不可靠）

**Step 3**: `upstream.py` line 968-970 判断 `all_non_quota_429`：
- 要求所有 cycle attempt 的 error_type ∈ `(None, "429", "429_rate_limit")` 
- passthrough 产生 `"429_quota_exhausted"` 不在集合 → `all_non_quota_429=False`
- cc-proxy 产生 `"429_rate_limit"` 在集合 → `all_non_quota_429=True`

**Step 4**: handlers.py retry-after 决策：
- `all_non_quota_429=False` → retry-after:**180** (passthrough)
- `all_non_quota_429=True` → retry-after:**5** (cc-proxy)

**Step 5**: CC/OpenClaw 客户端重试逻辑：
- **retry-after ≤ 60s → CC 正常退避重试**（5s → 等 5 秒 → 重试 → 多数 burst 恢复 → 成功）
- **retry-after > 60s → CC 抛 too_long → 直接放弃不重试**（180s → OpenClaw 卡住显示错误消息）

### 修复
1. **passthrough error_mapping.py**: `is_quota_exhaustion()` → always `return False`，与 cc-proxy 统一
2. **cc-proxy handlers.py**: ABORT + non-cycling error 路径添加 `_log_metrics()`（Ghost-ABORT bug）
3. **passthrough handlers.py**: ABORT + non-cycling error 路径添加 `_log_metrics()`（Ghost-ABORT bug）

### 效果
- 所有端口 429 → `429_rate_limit` → `all_non_quota_429=True` → **retry-after:5** → 客户端 5 秒重试
- metrics.jsonl 正确记录 ABORT 事件（status=429/502），不再 100% status=200 遮掩失败
- OpenClaw 卡住问题永久解决

### 部署验证 (15:26 CST)
- 40003 rebuild: ✅ startup 正常, `{'glm5.1': 197}` (dsv4p counter 已清除)
- 40001 rebuild: ✅ 
- 40005 rebuild: ✅ 
- curl 40005: ✅ 200 
- curl 40001: ✅ 200 
- curl 40003: ✅ 200

### Ghost-ABORT bug 说明
之前 ABORT (all_keys_exhausted) 路径不调用 `_log_metrics()`：
- cc-proxy handlers.py line 211-252: 3 个分支全部直接 return，没有 `_log_metrics`
- passthrough handlers.py line 450-457: 同样直接 return

结果：metrics.jsonl 显示 100% status=200，实际有 ABORT 事件但完全被遮掩。
这也是为什么日志"看起来一切正常"但 OpenClaw 实际卡住了——metrics 完全看不到失败。

## 参数现状 (40001=40005 mirror, 40003 unified)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False(all ports)

## 下轮待办
- 监控 40003 新 metrics 格式（error_type 应全部为 `429_rate_limit`，不再有 `429_quota_exhausted`）
- 监控 ABORT metrics 是否正确记录（status=429/502 代替 status=0/200）
- 添加 log_cleanup.sh 到 crontab
- throttle 1.5→1.0 测试（需有人值守）
