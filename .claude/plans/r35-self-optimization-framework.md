# R35: CC 链路自优化框架设计（CC→40000→40005/40001→41001）

## 链路核对

```
CC → :40000 dispatcher → :40005 cc-proxy(primary, 实验版) → MS slots → :41001 LiteLLM → ModelScope
                                              ↓ NV slots → :7894 mihomo → NVIDIA API (direct tunnel)

CC → :40000 dispatcher → :40001 cc-proxy(fallback, 稳定版) → :41001 LiteLLM → ModelScope (纯MS)
```

**关键澄清**：
- 40005 和 40001 是两个独立 cc-proxy 容器，**共享同一份 cc-proxy 代码**（`configs/proxy/cc-proxy/`），通过 docker-compose.yml env 控制差异
- 40001 当前用**旧共享镜像** `./proxy`（sync_config.sh L71-82），40005 用 `./proxy/cc-proxy`（新物理拆分镜像）
- 41001 是 LiteLLM（纯转发），不参与代码迭代，是两个 cc-proxy 的**共享上游**
- 40005 有 NV capability（env里有 NV_KEY/NV_PROXY_URL），40001 没有（纯MS fallback）

## 核心设计思想

**蓝绿部署 + 数据驱�� = 安全自优化**

两个 cc-proxy 容器天然对应两种角色：
- **40005 (PRIMART)**: 实验版 — 接收所有 opus/默认流量，承载最新参数/代码
- **40001 (FALLBACK)**: 稳定版 — 只在 40005 出问题或 sonnet 模型时才被路由到

优化迭代模式：
1. 在 40005 上试验新参数/代码 → 收集 metrics 对比
2. 数据证明有效 → 提升为"稳定" → 同步到 40001（使 40001 升级为新基线）
3. 数据证明无效 → 回滚 40005 → 40001 不受影响
4. 极端情况：40005 崩溃 → dispatcher 自动 fallback 到 40001（用户只感知 sonnet 路由）

## 框架设计：4 层结构

### Layer 1: Dispatcher 智能 Fallback（40000 增强）

当前 40000 只做静态路由（model 字段→端口），需要增强为**健康感知路由**：

```
请求到达 40000
  ├── 解析 model → 确定目标（primary=40005, fallback=40001）
  ├── 尝试连接目标
  ├── 目标连接失败/超时 → 自动切到另一个
  └── 成功 → 透传（同现有行为）
```

**改动**:`gateway_main.py` 的 `_relay()` 方法：
- 连接上游失败时（ConnectionRefused/timeout），**自动重试**另一个上游
- 记录 fallback 事件到日志（供优化循环分析）
- 不做健康检查 polling（保持无状态、零延迟）
- **仅影响连接失败场景**，model-based 路由逻辑不变

**代码量**：~20 行改动

### Layer 2: A/B Metrics 对比（cc-proxy 增强）

当前两个 cc-proxy 各自写 metrics 到**不同目录**：
- 40005: `./logs/proxy40005/metrics.{date}.jsonl`
- 40001: `./logs/proxy/metrics.{date}.jsonl`

优化循环需要能**对比两者**。增强：

1. **统一 metrics 格式**：两个容器 metrics 里加 `container_port` 字段（已有 `port` 字段）
2. **新建对比脚本** `scripts/compare_proxies.sh`：
   ```bash
   # 提取两个容器的 metrics，按时间窗口对比
   # 输出: 429率, avg TTFB, 成功率, NV使用率 等
   ```
3. **每小时 health score**：`scripts/proxy_health_score.py`
   - 读两个容器的 metrics 文件
   - 计算综合健康分（429率低→高分, TTFB低→高分, 成功率高→高分）
   - 写入 `configs/PROXY_HEALTH_SCORES.md`（供优化循环和人工查看）

### Layer 3: 40005↔40001 版本同步与提升机制

**当前问题**：40001 用旧共享镜像（`./proxy`），40005 用新独立镜像（`./proxy/cc-proxy`）。代码不一致！

**方案**：统一 40001 也用 `./proxy/cc-proxy` 镜像 + docker-compose env 控制差异：

```yaml
# docker-compose.yml 改动
auth_to_api_40001:
  build:
    context: ./proxy/cc-proxy      # ← 从 ./proxy 改为 ./proxy/cc-proxy
    dockerfile: Dockerfile
  environment:
    # ... 同现有的 cc-proxy 参数 ...
    # NV 不启用（纯 MS fallback）
    NV_NUM_KEYS: "0"               # ← 关键差异：40001 没有 NV
```

**版本提升流程**（写进 `memory/cron-optimization-loop.md`）：

```
┌─────────────────────────────────────────────────────┐
│  1. 40005 实验新参数/代码（env 或 code）            │
│  2. collect 1-2 小时 metrics                        │
│  3. compare_proxies → 计算 health_score             │
│  4. score 提升 AND 无 crash?                         │
│     YES → 同步提升到 40001:                        │
│            a. 更新 40001 的 docker-compose env       │
│            b. rebuild 40001                          │
│            c. smoke test 40001                      │
│            d. 更新 DEPLOY_STATUS.md                 │
│     NO  → 回滚 40005:                              │
│            a. 恢复 docker-compose env               │
│            b. rebuild 40005                          │
│            c. 40001 不受影响                          │
└─────────────────────────────────────────────────────┘
```

### Layer 4: 可调参数自动寻优（优化循环脚本增强）

**当前**：优化循环依赖 headless claude agent，不可控因素多。

**增强**：新增 `scripts/auto_tune.sh`，纯 bash + python 脚本，不依赖 AI agent：

```bash
# auto_tune.sh — 每轮执行：
# 1. 读 PROXY_HEALTH_SCORES.md（Layer 2 产出）
# 2. 识别最差维度（429率? TTFB? 超时?）
# 3. 查参数规则表（configs/TUNE_RULES.md）
# 4. 生成调整建议 → 写入 configs/NEXT_ROUND.md
# 5. 小范围参数自动应用 + rebuild 40005
# 6. 大范围参数/代码变更 → 写建议，等人工或 AI agent 确认
```

**参数规则表** `configs/TUNE_RULES.md`：

```markdown
| 指标异常 | 阈值 | 自动调参 | 新值 | 理由 |
|---------|------|---------|------|------|
| 429率 > 30% | -- | MIN_OUTBOUND_INTERVAL_S += 0.5 | 2.0→2.5 | burst 缓解 |
| 429率 < 5% | -- | MIN_OUTBOUND_INTERVAL_S -= 0.3 | 2.0→1.7 | 加速吞吐 |
| 超时率 > 10% | -- | UPSTREAM_TIMEOUT += 15 | 60→75 | 网络抖动 |
| 超时率 < 1% | -- | UPSTREAM_TIMEOUT -= 10 | 60→50 | 减少阻塞 |
| NV TTFB > 8s | -- | 记录，不改参 | — | NV 问题需人工 |
| MS TTFB > 10s | -- | 记录，不改参 | — | ModelScope 问题 |
```

**安全边界**：
- MIN_OUTBOUND_INTERVAL_S: [0.5, 5.0]
- UPSTREAM_TIMEOUT: [30, 120]
- PROXY_TIMEOUT: [120, 600]
- 超出范围的变化需要人工确认

## 实现文件清单

| # | 文件 | 改动类型 | 说明 |
|---|------|---------|------|
| 1 | `configs/proxy/dispatcher/gateway_main.py` | 修改 | 增加连接失败自动 fallback |
| 2 | `configs/docker-compose.yml` | 修改 | 40001 build context 改为 cc-proxy，加 NV_NUM_KEYS=0 |
| 3 | `scripts/sync_config.sh` | 修改 | 删掉 40001 的旧 shared gateway 同步条目 |
| 4 | `scripts/compare_proxies.sh` | 新建 | 两个 proxy 的 metrics 对比分析 |
| 5 | `scripts/proxy_health_score.py` | 新建 | 自动计算 proxy 健康分 |
| 6 | `scripts/auto_tune.sh` | 新建 | 参数自动寻优脚本 |
| 7 | `configs/TUNE_RULES.md` | 新建 | 参数调整规则表 |
| 8 | `memory/cron-optimization-loop.md` | 新建 | 优化循环详细流程文档 |
| 9 | `configs/DEPLOY_STATUS.md` | 更新 | 记录 R35 架构变更 |
| 10 | `CLAUDE.md` | 更新 | 补充框架文档 |

## 执行顺序

### Phase 1: 基础对齐（安全，无破坏性）
1. 40001 build context 统一为 `./proxy/cc-proxy`（消除代码不一致）
2. 40001 加 NV_NUM_KEYS=0（显式声明纯 MS 模式）
3. 更新 sync_config.sh（删旧 shared gateway 条目）
4. rebuild 40001 + smoke test 验证

### Phase 2: Dispatcher 智能化
5. 40000 gateway_main.py 增加连接失败自动 fallback 逻辑
6. rebuild 40000 + 验证（手动测试：停 40005 → 看 40000 是否自动 fallback）

### Phase 3: Metrics 对比
7. 新建 compare_proxies.sh + proxy_health_score.py
8. 手动执行一次，验证输出合理

### Phase 4: 自动寻优
9. 新建 TUNE_RULES.md + auto_tune.sh
10. 新建 cron-optimization-loop.md（优化循环文档）
11. 手动执行一次 auto_tune.sh，验证参数建议合理

### Phase 5: 文档更新
12. 更新 DEPLOY_STATUS.md, CLAUDE.md

## 风险控制

- **Phase 1 风险**：40001 切到 cc-proxy 镜像后行为不一致。**缓解**：先 smoke test，对比 /health 输出
- **Phase 2 风险**：dispatcher fallback 逻辑引入 bug。**缓解**：只改连接失败路径，成功路径零改动
- **Phase 3 风险**：metrics 对比逻辑有误。**缓解**：纯只读脚本，不写配置
- **Phase 4 风险**：auto_tune 误调参数。**缓解**：有安全边界 + 需 rebuild 才生效 + 40001 作为 safetynet
- **通用风险**：rebuild 期间短暂不可用。**缓解**：一次只 rebuild 一个容器，40001 始终在线
