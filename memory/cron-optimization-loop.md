# cron-optimization-loop.md — 优化循环详细流程（R35）

每 30 分钟由 cron 唤起的 headless agent（`run_optimization_loop.sh`）执行一轮。
本文件定义每轮的详细步骤和决策逻辑。

## 每轮流程

### Step 1: 前置检查
```bash
git pull --rebase --autostash
curl -sf http://127.0.0.1:40005/health || exit 0  # 40005 不健康就跳过
curl -sf http://127.0.0.1:40001/health || echo "WARN: 40001 also unhealthy"
```

### Step 2: 采集数据
```bash
# 两个容器的 metrics
docker logs --since 30m auth_to_api_40005 2>/dev/null | tail -200
docker logs --since 30m auth_to_api_40001 2>/dev/null | tail -200

# 运行健康评分
python3 scripts/proxy_health_score.py

# 对比两个 proxy
bash scripts/compare_proxies.sh last2h

# quota 检查（如果脚本存在）
bash scripts/check_quota_balance.sh 2>/dev/null || echo "no quota script"
```

### Step 3: 分析 + 决策

读 `configs/PROXY_HEALTH_SCORES.md` 中的 verdict：
- **PROMOTE_40005**: 40005 表现好 → 执行版本提升
- **ROLLBACK_40005**: 40005 表现差 → 执行回滚
- **STABLE**: 无操作 → 只更新接力文件
- **INSUFFICIENT_DATA**: 无操作 → 只更新接力文件
- **NO_DATA_40005**: 无操作 → 检查 40005 是否有流量

### Step 4: 版本提升（PROMOTE 流程）

当 40005 连续 2+ 小时 score > 40001 + 5：

1. 读 40005 当前 docker-compose env 参数
2. 将 40005 的参数同步到 40001 的 docker-compose env
   - **不同步的参数**：NV_NUM_KEYS/NV_KEY*/NV_PROXY_URL（40001 保持纯 MS）
   - **同步的参数**：MIN_OUTBOUND_INTERVAL_S, UPSTREAM_TIMEOUT, PROXY_TIMEOUT, 等
3. 重建 40001: `docker compose up -d --build --force-recreate auth_to_api_40001`
4. smoke test 40001
5. 更新 DEPLOY_STATUS.md
6. 记录到 NEXT_ROUND.md

### Step 5: 回滚（ROLLBACK 流程）

当 40005 连续 1+ 小时 score < 40001 - 10：

1. 读 40001 当前参数作为"基线"
2. 恢复 40005 的参数到 40001 的基线
   - NV 相关参数保持不变（40005 仍可使用 NV）
3. 重建 40005: `docker compose up -d --build --force-recreate auth_to_api_40005`
4. smoke test 40005
5. 更新 DEPLOY_STATUS.md
6. 记录到 NEXT_ROUND.md

### Step 6: 自动参数调整

运行 `bash scripts/auto_tune.sh --suggest`：
- 生成参数建议写入 `configs/NEXT_ROUND.md`
- **不自动 apply**，留给下一轮 agent 或人工确认
- 429 率 > 30% 且连续 2 轮 → 可以 `--apply`（安全边界内的参数调整）

### Step 7: 写接力文件

更新 `configs/NEXT_ROUND.md`：
- 本轮观察到的指标变化
- 执行了什么操作
- 建议下一轮关注什么
- 累积变化仪表板

### Step 8: Push

```bash
git add -A
git commit -m "R35: optimization round — [brief summary]"
git push
```

## 版本提升参数对照表

| 参数 | 40005→40001 同步? | 说明 |
|------|-------------------|------|
| MIN_OUTBOUND_INTERVAL_S | ✅ 是 | 通用节流参数 |
| UPSTREAM_TIMEOUT | ✅ 是 | 通用超时参数 |
| PROXY_TIMEOUT | ✅ 是 | 通用总超时 |
| CHARS_PER_TOKEN_ESTIMATE | ✅ 是 | 通用估算 |
| NUM_KEYS | ❌ 否 | 物理约束，不变 |
| NUM_VARIANTS_* | ❌ 否 | 物理约束，不变 |
| NV_NUM_KEYS | ❌ 否 | 40001 纯 MS |
| NV_KEY* | ❌ 否 | 40001 无 NV |
| NV_PROXY_URL | ❌ 否 | 40001 无 NV |
| NV_BASEURL | ❌ 否 | 40001 无 NV |

## 异常检测

| 异常 | 处理 |
|------|------|
| 40005 连续 3 次 health check 失败 | 重建 40005，记入 NEXT_ROUND.md |
| 40001 health check 失败 | 重建 40001 + 告警（手动干预） |
| 两个 proxy 都不健康 | 只告警，不自动改配置 |
| MS 429 全量（140 dep 都 429） | 不做代码变更，只建议减少频率 |
| NV API 全球故障 | 建议 NV_NUM_KEYS=0，等恢复 |
