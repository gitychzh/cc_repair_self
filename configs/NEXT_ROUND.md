# Round R35.2 — 2026-06-21

## R35.2 Round 3 数据分析总结（03:20-03:39 CST, throttle=1.5 纯MS）

### 数据验证结果 (225 requests, ~19 minutes)
- **100% 成功率**，0 429，0 ABORT，0 空响应
- **100% 零 cycling**（所有请求一次通过，无需 key cycling）
- **avg TTFB 5.8s** (p50=4.9s, p75=7.0s, p90=10.6s, p95=13.1s)
- **finish_reason**: tool_calls=157(69.8%), length=27(12%=测试curl), stop=2
- **tool_truncation**: 202/225(89.8%) 有 truncation 数据（CC 正常传 truncated tool 内容）
- **TTFB by input size**: 0-10k=2.1s, 10-30k=5.5s, 30-50k=5.0s, 50-80k=6.8s, 80-130k=7.6s
- **无时间维度波动**: 各5分钟窗口 TTFB 4.6-7.4s，正常波动

### 40001 (stable/baseline) 对比
- 03:22 的 2 个空200响应来自旧容器（NV_ENABLED=True 的旧版 40001）
- 03:27 重建后 40001=40005 mirror，后续请求全部正常
- 40001 空响应率 40%(2/5) 仅因旧容器遗留 + 样本量极小

### 核心结论
**系统已进入稳定黄金期**。NV 禁用 + throttle=1.5 的组合效果极佳：
- 从 NV-era avg TTFB ~60s → 纯MS 10s → throttle=1.5 5.8s（**90%改善**）
- 429 cycling 从 49% → 30% → **0%（100%一次通过）**
- 空响应从 12.1% → **0%**

### 无人值守模式决策（稳定优先）
- **不进一步降低 throttle**：1.5s 已达 0 cycling + 100% 成功，降低风险不可控
- **不重启 NV**：NV API 仍不可用（成功率 8.7%，超时 55.8%）
- **维持当前参数观察**：等待更多数据积累和用户醒来后的决策

## 累计优化效果

| 指标 | 原始(NV=5, throttle=2.0) | R35.1(NV=0, throttle=2.0) | R35.2(throttle=1.5) |
|------|--------------------------|---------------------------|---------------------|
| avg TTFB | ~60s(NV拖慢) | 10.0s | 5.8s |
| 429 cycling率 | N/A | 49% → 30% | **0%** |
| success rate | 100% | 100% | 100% |
| empty output | 12.1% | 0% | 0% |
| zero-cycling率 | ~50% | 73% | **100%** |

## Round 4 待办
- 继续监控稳定性（无人值守8h+）
- 如果发现 429 突增或 fallback 事件 → 立即回退到 throttle=2.0
- 用户醒来后讨论：是否测试 throttle=1.0（需有人值守）
- NV API 恢复监测（可用 compare_proxies.sh 定期检查）
- 考虑 log rotation（proxy 日志无清理，~1.2MB/天无上限）

## 参数现状 (40001=40005 mirror)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS)
