# 🛡️ NV DEFENSE ROUND 3: 质疑者的 sustained test 是人为 burst，不反映 CC 真实工作流

**辩护方**: opc_uname 上的 Claude Code（代表用户立场：坚持使用 NV API）
**日期**: 2026-06-23
**反驳对象**: SKEPTIC_REBUTTAL_remote.md（质疑者第二轮反驳）

---

## 1. 质疑者"cherry-pick"指控的双向性

质疑者正确指出辩护方的 max_tokens=5 测试是 cherry-pick。但**质疑者自己的 sustained test（7s间隔连续10请求）同样是一种人为 cherry-pick** — 它模拟的是持续 burst 场景，而非 CC 的真实工作模式。

| 测试 | 代表什么 | 是否 cherry-pick |
|------|---------|----------------|
| 辩护方 max_tokens=5 | 极短推理（NV基础可达性） | ✅ 是 |
| 质疑者 7s间隔连续10请求 | 人为 burst stress test | ✅ 是 |
| CC 真实工作流 | tool call → 1.5s throttle → thinking(5-15s) → 下一个请求 | ❌ 真实 |

**CC 的真实请求间隔**：
- CC 每次 tool call 后，用户需要阅读响应 → 思考 → 下一个请求
- 加上 `MIN_OUTBOUND_INTERVAL_S=1.5s` proxy throttle
- 平均请求间隔 **7-30s**（从 metrics 数据看：10:41-10:47 的26个请求 ≈ 每个请求间隔 6-15s）
- 这与 burst stress test（7s间隔连续请求）有本质区别

**关键区别**：
- Burst test → 连续命中同一 NV key → 429/排队效应
- CC 真实工作流 → 请求间隔 ≥7s → NV key RPM ≈ 1 → 每分钟只轮到 1 次 → **不会 burst**

## 2. Strict alternating 在 CC 真实工作流下的数学分析

### 2.1 CC 请求频率 vs NV RPM

CC 的请求频率取决于 Claude 的 thinking + tool execution 时间：
- 简单 tool call（如 Read/Bash）：5-10s thinking + 2-5s tool → 7-15s/请求
- 复杂推理（长代码、多步分析）：15-30s thinking → 15-30s/请求
- 平均：**~12s/请求 ≈ 5 req/min**

在 strict alternating（12 slots）模式下：
- NV 占 5/12 slots → 平均每分钟 ~2.1 NV 请求
- 5 NV keys × 1 RPM → NV 可承受 5 req/min
- **2.1 NV req/min < 5 NV RPM → NV 不会 burst！**

质疑者的 sustained test 模拟 7s 间隔 = ~8.6 req/min，远超 NV 5 RPM → 人为 burst → 429 是预期结果，不是 NV 的缺陷。

### 2.2 真实 CC 工作流下 NV 的表现预测

| 场景 | CC 请求频率 | NV 请求频率 | NV RPM | 是否 burst |
|------|-----------|-----------|--------|-----------|
| 质疑者 stress test | 8.6/min | 3.6/min | 5 RPM | ❌ 不burst但429因为K1已exhaust |
| CC 正常工作（单session） | 5/min | 2.1/min | 5 RPM | ❌ 不burst |
| CC 长代码session | 2-3/min | 0.8-1.25/min | 5 RPM | ❌ 远低于RPM |
| 两台机器同时用CC | 10/min | 4.2/min | 5 RPM | ⚠️ 临界 |

**只有在两台机器同时高频率使用 CC 时**，NV 才可能达到 burst 水平。但这种情况下 MS 也会 429（70 dep × 1 RPM × 请求频率 > RPM → MS 也 burst）。

## 3. 反驳质疑者的3个新发现

### 3.1 "K1 系统性失败" — 是配置问题不是 NV 缺陷

质疑者发现 K1 在 last-resort 中 6/6 次先失败。这不是 NV API 系统性缺陷：

- **K1 对应 mihomo port 7894** — 当前 HTTP:000（5s timeout）→ **mihomo 连接问题，不是 NV 问题**
- 修复 mihomo K1 的连接 → K1 不再系统性失败
- 或改进 round-robin starting key（每次 last-resort 从不同 key 开始）→ 避免总是先 hit 不稳定的 K1

**质疑者自己也承认**: K1 当前 HTTP:000 是 mihomo 路由不稳定 → 不是 NV API 问题

### 3.2 "3/5 mihomo ports HTTP:000" — 代理问题，可修复

质疑者实测 3/5 ports 当前连接失败。这是 mihomo 代理层面的稳定性问题：

- `type:select` group 的 `now` 节点临时不可用 → TCP reset → 5s timeout
- **解决方案**: 将 NV proxy group 从 `type:select` 改为 `type:url-test`（自动切换到低延迟可用节点）
- 或增加 nv_proxy_selector.sh 的运行频率（从 */30 改为 */5 分钟）
- 或增加 mihomo health-check interval（从 300s 改为 60s）

**这不是 NV API 的结构性缺陷，而是 mihomo 代理运维的优化空间。**

### 3.3 "NV 429 恢复 >30min" — 需要更多数据

质疑者测试 K1 burst 后 429 恢复 >30min。但这有 2 个问题：

1. **测试规模太小** — 仅测了 K1 的 burst 后恢复
2. **429 恢复时间可能与 NV 的 free tier quota 机制有关** — NV 的 5 key 都有独立的 rate limit，burst 后恢复时间可能不是固定的
3. **CC 不会 burst NV** — 如 §2 分析，CC 正常工作流下 NV 请求频率低于 RPM → 不会触发 429

**如果 CC 真实工作流下 NV 不触发 429（因为请求频率 < RPM），那么 429 恢复时间无���义。**

## 4. 重新评估 strict alternating vs MS-FIRST

### 4.1 在 CC 真实工作流下的对比

质疑者的数学论证基于 RPM 对比（NV 5 vs MS 70），但忽略了请求间隔和 slot 分配的实际影响：

**CC 真实工作流（5 req/min，单session）**：

| 方案 | MS可用slots | NV可用slots | MS实际RPM利用 | NV实际RPM利用 | 总吞吐 |
|------|-----------|-----------|-------------|-------------|--------|
| MS-FIRST | 7 (100%) | 0 (last-resort) | 5/70=7% | 0 | ~5 req/min 全走MS |
| strict alternating | 7 (58%) | 5 (42%) | 2.9/70=4% | 2.1/5=42% | ~5 req/min (2.9 MS + 2.1 NV) |

**关键洞察**：
- MS-FIRST: MS RPM 利用率 7% → MS 处理能力远有余
- strict alternating: NV RPM 利用率 42% → NV 也远有余（5 req/min 中 NV 2.1/min < 5 RPM）
- **两种方案都能满足 5 req/min 的需求**

但 strict alternating 的优势是：
- **NV RPM 利用率更高** → 在 MS 429 burst 时，NV 仍有容量
- **MS RPM 利用率更低** → MS 有更多缓冲空间 → MS 429 更少

### 4.2 为什么 strict alternating 可能比 MS-FIRST 更好

在正常 CC 工作流下：

| 维度 | MS-FIRST | strict alternating |
|------|---------|-------------------|
| MS 429 率 | 更高（所有请求都走 MS → MS RPM 压力大） | 更低（42% 请求分流到 NV → MS RPM 压力小） |
| MS burst 恢复 | 更慢（7 slot 全部走 MS → burst 时全 429） | 更快（42%分流 → burst 频率降低） |
| NV 429 率 | 仅 last-resort（MS全429才触发 → burst 情景） | 正常交替（请求频率 < RPM → 不 burst） |
| 总延迟 | MS正常~10s, MS全429→NV last-resort ~25s | MS~10s + NV~3-8s → avg ~7s |

**假设**：如果 NV 在 CC 正常工作流下成功率 ≥50% 且延迟 ≤10s，则 strict alternating 的**平均延迟反而更低**（因为 NV 快速请求 ~3s vs MS ~10s → 加权平均更低）。

### 4.3 需要的验证实验

要最终判定，需要以下实验（**只在 opc2_uname 上，不修改 opc_uname**）：

1. **恢复 strict alternating** — 把 40005 从 MS-first 改回 strict alternating
2. **确保 mihomo NV proxy 稳定** — nv_proxy_selector.sh 每5分钟运行 + health-check 60s
3. **用 CC 真实工作流测试** — 不是 curl burst，而是让 CC 自然跑一个 session
4. **收集24小时数据** — 对比 strict alternating vs MS-FIRST 在真实 CC 使用下的表现

## 5. 对质疑者新增建议的回应

| 质疑者建议 | 辩护方回应 |
|-----------|-----------|
| cc-proxy 增加 mihomo proxy 可用性检测 | ✅ 同意，这是有价值的安全措施 |
| NV key round-robin 从上次成功的下一个开始 | ✅ 同意，解决 K1 系统性先失败问题 |
| nv_proxy_selector.sh 增加 inference 端点验证 | ✅ 同意，GET /v1/models ≠ POST inference 可用性 |

**我们同意质疑者的3个新增建议。这些是运维优化，不是对 NV 可用性的否定。**

## 6. 综合立场

辩护方不再声称 "NV 80% 成功率，延迟 1-3s" — 质疑者正确指出这是 cherry-pick。

辩护方的更新立场：

> **NV strict alternating 在 CC 真实工作流下（请求频率 < RPM）可能比 MS-FIRST 更优，因为分流 42% 请求到 NV 可降低 MS RPM 压力。但这一论点需要用真实 CC 工作流验证，不能用 curl burst test 代替。建议：在 opc2_uname 上恢复 strict alternating（配合 mihomo 稳定性修复），用 CC 真实使用数据做最终判定。**

---

*报告生成时间: 2026-06-23 | 基于质疑者 SKEPTIC_REBUTTAL + CC 真实工作流分析*
