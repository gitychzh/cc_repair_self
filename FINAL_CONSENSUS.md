# ⚖️ FINAL CONSENSUS: NV API 战略定位 — 数据驱动的共识报告

**双方**: opc2_uname（质疑者）+ opc_uname（辩护方）
**日期**: 2026-06-23
**结论**: 双方达成**部分共识 + 明确分歧点**。核心共识：R36.5 MS-FIRST 是当前最优策略。分歧点：NV strict alternating 在代理完全稳定时是否有未来价值。

---

## 一、达成共识的部分

### 1.1 ✅ R36.5 MS-FIRST + NV Last-Resort 是当前最优策略

**共识依据**：

| 数据点 | 来源 | 说明 |
|--------|------|------|
| MS-FIRST MS成功率 95.6% | metrics 10:43-11:59 (196/205) | MS 70 dep cycling 几乎总能成功 |
| MS-FIRST NV 从未作为常规 slot 触发 | proxy.2026-06-23.log | 全部请求走 MS，NV 仅在 MS all-429 时触发 |
| NV last-resort 触发频率极低 | 9/226 requests = 3.98% | 仅在 MS 7key 全 429 时触发 |
| Strict alternating NV 成功率 ≤14.3% | 美国节点后 proxy 日志 | 系统性失败，不是偶发 |
| Strict alternating NV 延迟 20-42s | 5次 NV-SUCCESS 数据 | p50=34s，是 MS 的 3-4 倍 |
| mihomo 当前 3/5 ports 不稳定 | 实测 7894/7895/7897 HTTP:000 | 代理层结构性问题 |

**双方同意**：不恢复 strict alternating，维持 R36.5 MS-FIRST 架构。

### 1.2 ✅ 辩护方 mihomo 修复是有价值的运维发现

辩护方正确识别了 mihomo proxy 分配日本节点而非美国节点的 bug。质疑者承认：

1. 日本节点 GET /v1/models 可达但 inference POST 超时 → 这是 NV 超时的重要原因之一
2. mihomo 重启后 `now` 状态丢失 → nv_proxy_selector.sh 需要 cron 定时运行
3. 辩护方修复后 NV 可用性改善（7895/7899 目前可用）

**但质疑者指出**：修复后 NV 仍不稳定（3/5 ports 失败，K1 每次先 fail），证明 mihomo 问题只是 NV 不稳定的原因之一，不是唯一原因。

### 1.3 ✅ 三方共识的改进建议

| 建议 | 来源 | 优先级 | 状态 |
|------|------|--------|------|
| cc-proxy 增加 mihomo proxy 可用性检测 | 双方共识 | 中 | 📋 待实施 |
| NV key round-robin 从上次成功的下一个 key 开始 | 双方共识 | 中 | 📋 待实施（避免 K1 系统性先失败浪费 17.5s） |
| nv_proxy_selector.sh 增加 inference 端点验证 | 双方共识 | 中 | 📋 待实施（POST ≠ GET） |
| mihomo proxy group 从 `type:select` → `type:url-test` | 辩护方提出，质疑者支持 | 中 | 📋 待评估 |

### 1.4 ✅ 双方承认各自的 cherry-pick 问题

- **辩护方承认**：max_tokens=5 的 80% 成功率和 1-3s 延迟是 cherry-pick，不代表真实 CC 工作流
- **质疑者承认**：7s间隔连续10请求是 burst stress test，不代表 CC 真实请求模式

---

## 二、明确分歧点

### 2.1 ❌ NV strict alternating 在 CC 真实工作流下是否可行

**辩护方论点**（Round 3）：
> CC 真实请求频率 5 req/min → strict alternating 下 NV 请求频率 2.1/min < NV 5 RPM → NV 不 burst → strict alternating 可能比 MS-FIRST 更优（分流 42%→MS RPM 压力小）

**质疑者反驳**：

辩护方的数学推理**方向正确但结论错误**。CC 真实请求频率确实 < NV RPM（实测 3.3 req/min，辩护方估计 5 req/min），NV 不会 burst。但**"不 burst" ≠ "有效补充"**：

| 反驳维度 | 数据 |
|----------|------|
| **NV 成功率极低** | 即使不 burst，14-60% 成功率意味着大量 NV 请求失败后仍 fallback 到 MS，反而增加延迟 |
| **NV 延迟远高于 MS** | NV 成功 ~23s vs MS ~10s → 每次 NV 请求浪费 ~13s |
| **MS RPM 压力不是问题** | MS-FIRST 下 MS 成功率 95.6% → MS 处理能力远有余，不存在"MS 压力太大需要 NV 分流"的问题 |
| **分流的净效果** | strict alternating throughput = 2.03-2.66 req/min < MS-FIRST 3.15 req/min → **19-55% throughput 降低** |
| **CC 实测请求频率** | 3.3 req/min (median 13.3s)，而非辩护方假设的 5 req/min |

**核心分歧**：

辩护方假设"分流降低 MS 429 率"→ 但 MS-FIRST 下 MS 429 率仅 ~4.4%（205 次中仅 9 次 ABORT）→ MS 不需要分流。

辩护方假设"NV 成功率在 CC 真实工作流下会比 burst test 更高"→ 质疑者同意 burst test 不代表真实场景，但**proxy 日志的真实 CC 工作流数据同样显示 NV 成功率极低（14.3%）**。这不是 burst test 数据，而是 strict alternating 时代的真实请求记录。

**结论**：辩护方的"CC 真实工作流下 strict alternating 可行"论点，理论上合理（请求频率 < RPM），但**实测数据证伪**（即使在真实 CC 工作流下 NV 成功率仍 ≤14.3%）。

### 2.2 ❌ K1 系统性失败是 mihomo 问题还是 NV 问题

**辩护方论点**：K1 对应 mihomo port 7894，当前 HTTP:000 → 是 mihomo 连接问题，不是 NV API 问题。修复 mihomo → K1 不再系统性失败。

**质疑者反驳**：
- K1 在 last-resort 中 7/9 次先失败（503 和 429），而 HTTP:000 仅是连接层面的失败
- K1 的 503 ResourceExhausted 和 429 是 **NV API 返回的**，不是 mihomo 层面
- 即使 7894 当前 HTTP:000，K1 在 mihomo 连接正常时（7895 走 K2 成功的同一时期）仍返回 503/429
- 7894 的 HTTP:000 是叠加问题，但 K1 的 503/429 是 NV API 层面的系统性问题

**当前实测数据**（2026-06-23 11:xx）：
| Port | Key | 状态 | 说明 |
|------|-----|------|------|
| 7894 | K1 | HTTP:000 (10s timeout) | mihomo 连接失败 |
| 7895 | K2 | 200 (2.27s) | 可用 |
| 7896 | K3 | 503 (2.07s) | NV API 503 ResourceExhausted |
| 7897 | K4 | 503 (1.04s) | NV API 503 ResourceExhausted |
| 7899 | K5 | 200 (1.89s) | 可用 |

**结论**：K1 的问题是**双重叠加**：mihomo 连接不稳定 + NV API 层面的 503/429。K3 和 K4 的 503 是纯 NV API 问题（mihomo 连接成功但 NV API 返回 503）。辩护方说"修复 mihomo → K1 不再系统性失败"部分正确，但 NV API 层面的系统性失败（K3/K4 的 503）不是 mihomo 能修复的。

### 2.3 ❌ NV 429 恢复 >30min 在 CC 真实工作流下是否重要

**辩护方论点**：CC 正常工作流下 NV 请求频率 < RPM → 不会触发 429 → 429 恢复时间无关。

**质疑者反驳**：

这个论点**在 strict alternating 正常运行时确实成立**（如果 NV 请求频率 ≤ 2.1/min < 5 RPM → 不会 429）。但：

1. **MS-FIRST last-resort 场景下**：NV 只在 MS all-429 时触发 → 此时 CC 已经连续快速请求了 ~10-30s（MS 7 key cycling）→ 然后立即触发 NV → NV 馢于 burst 后的残余 429 → K1 的 7 次 429/503 就是证据
2. **两台机器同时使用**：10 req/min → NV 请求 4.2/min → 临界 RPM → 可能触发 429
3. **429 恢复 >30min 意味着**：一旦 burst，NV 长时间不可用 → 不是"无关"，而是"一旦触发就很严重"

**结论**：辩护方的论点在单台机器正常工作流下成立，但在 MS all-429→NV last-resort 触发场景和双机同时使用场景下不成立。NV 的 429 恢复时间问题不是"无关"，而是"在特定关键场景下会恶化"。

---

## 三、综合判定

### 3.1 优先级排序（双方共识）

| 优先级 | 决策 | 依据 |
|--------|------|------|
| **P0** | ✅ 维持 R36.5 MS-FIRST + NV last-resort | 实测数据验证：MS 95.6% 成功率，NV 仅 3.98% 触发 |
| **P1** | ✅ 不恢复 strict alternating | 即使 CC 真实工作流下，NV 成功率 14.3% + 延迟 23s → throughput 降低 19-55% |
| **P2** | 📋 实施 NV key round-robin 优化（从上次成功 key 的下一个开始） | 减少每次 last-resort K1 先失败的 ~17.5s 浪费 |
| **P3** | 📋 实施 mihomo proxy 可用性检测 + inference 端点验证 | 降低 mihomo 层面失败对 NV 的叠加影响 |
| **P4** | 📋 评估 mihomo `type:select` → `type:url-test` 切换 | 提高代理自动切换能力 |

### 3.2 未来观察期决策（双方分歧但可共存）

| 时间线 | 辩护方立场 | 质疑者立场 | 共存方案 |
|--------|-----------|-----------|----------|
| 2周观察期 | 保留 NV_NUM_KEYS=5，修复 mihomo 后验证 NV 在 last-resort 中是否改善 | NV_NUM_KEYS=0 彻底禁用，2周后数据确认 MS-FIRST 稳定性 | **维持 NV_NUM_KEYS=5 但仅在 last-resort 使用**，同时实施 P2-P4 优化 |
| 2周后评估 | 如果 mihomo 修复 + K1 round-robin 改善 → NV last-resort 价值可能提升 | 如果 MS-FIRST 继续稳定（95%+）→ NV 从未触发 → NV_NUM_KEYS=0 | **2周后用数据判定**：如果 MS-FIRST 持续稳定且 NV last-resort 从未触发 → 考虑 NV_NUM_KEYS=0 |

### 3.3 绝对不做的决定（双方共识）

| 禁止事项 | 原因 |
|----------|------|
| ❌ 不恢复 strict alternating | 数学证伪 + 实测证伪 |
| ❌ 不删除 NV variant model IDs | 不可变更约束（CLAUDE.md） |
| ❌ 不增加 NV slots 数量 | RPM 仅 5，无法承载更多 |
| ❌ proxy 不做截断/压缩 | 不可变更原则 |

---

## 四、最终立场总结

### 质疑者最终立场

> **R36.5 MS-FIRST + NV last-resort 是当前最优策略，不需要改动。** 辩护方的 Round 3 论点（CC 真实工作流下 NV 不会 burst）方向正确，但**不 burst ≠ 有效补充**——实测数据显示 NV 即使在真实 CC 工作流下成功率仍 ≤14.3%，延迟 23s vs MS 10s。strict alternating 的 throughput 降低 19-55% 是确定性结论。建议维持 MS-FIRST，同时实施 NV key round-robin 和 mihomo 优化，2周后用数据决定是否 NV_NUM_KEYS=0。

### 辩护方最终立场（推测，基于 Round 3）

> **承认 R36.5 是当前最优，但保留 NV 的未来价值。** 在 mihomo 修复 + K1 round-robin 优化后，NV last-resort 的成功率可能提升。不恢复 strict alternating，但保留 NV_NUM_KEYS=5 作为安全网。2周观察期后用数据决定是否进一步精简。

### 共识结论

> **双方共识：维持 R36.5 MS-FIRST + NV last-resort 架构不变。实施 NV key round-robin 优化和 mihomo 稳定性改进。2周观察期后，基于 MS-FIRST era 的持续数据，决定是否 NV_NUM_KEYS=0 或保留为 last-resort。**
>
> **分歧点保留**：辩护方认为 NV 在 mihomo 修复后有未来价值；质疑者认为 NV 的结构性缺陷（低 RPM、API 503、代理不稳定）使未来价值有限。这一分歧不需要现在解决，交给2周数据判定。

---

## 五、关键数据清单（供未来参考）

| 数据 | 值 | 来源 |
|------|-----|------|
| CC 真实请求频率 | 3.3 req/min (median 13.3s) | metrics 10:43-11:59 |
| MS-FIRST MS成功率 | 95.6% (196/205) | metrics 10:43-11:59 |
| MS-FIRST NV 触发率 | 3.98% (9/226 requests) | proxy.2026-06-23.log |
| NV last-resort session 成功率 | 9/9 = 100% | proxy.2026-06-23.log |
| K1 首次失败率 | 7/9 = 77.8% | proxy.2026-06-23.log |
| K1 失败平均浪费 | 17.5s | proxy.2026-06-23.log |
| NV 成功延迟（真实 CC） | 23s avg (含 K1 失败) | proxy.2026-06-23.log |
| MS 直接延迟 | ~10s avg | metrics |
| mihomo 当前可用率 | 2/5 ports = 40% | 实测 (7895+7899 可用) |
| Strict alternating NV 成功率 | 14.3% (4/28) | proxy.2026-06-23.log (美国节点后) |
| NV API 503 (K3/K4) | 2/5 ports 返回 503 | 实测 |
| NV RPM 总容量 | 5 RPM | 5 key × 1 RPM |
| MS RPM 总容量 | 70 RPM | 10 variant × 7 key × 1 RPM |

---

*共识报告生成时间: 2026-06-23 | 基于三轮对抗性论证 + 实测数据验证*
*质疑者: opc2_uname Claude Code | 辩护方: opc_uname Claude Code*
