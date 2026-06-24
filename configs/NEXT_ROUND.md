# NEXT_ROUND — 接力文件（交替优化框架 R41+）

> 上一轮: R41 (cc1→cc2, 2026-06-25 03:55) — hm40006 DB 持久化修复
> 下一轮: cc2→cc1（cc2 那边的 Claude 分析本机日志优化 cc1）

## 上一轮成果（R41, cc1 优化 cc2, commit 8867e7a）

**问题**：cc2 hm40006 的 hm_metrics jsonl 有 150 行但 hermes_logs.hm_requests 表只有 6 行——R40 可观测性哑火。
**根因**：`upstream.py execute_request()` 末尾的 `_log_metrics({残缺dict})` 缺 NOT NULL `timestamp`/`ts` 和 `duration_ms`/`fallback_tiers_used`，一个残缺 dict 毒死整批 INSERT（rollback），56% fallback+16.7% 全失败导致几乎每 batch 都中。
**修复**：删掉 upstream.py 的残缺双写（handlers.py all_keys_exhausted 分支已有完整写入）。
**验证**：rebuild hm40006 后 2 个真实请求均正确落库，零 [HM-DB] 报错。

## 下轮候选（按优先级 + 数据支撑排序）

### 1. [cc2] hm_tier_attempts 表 0 行（本轮遗留）
- 成功路径 handlers.py 未填充 `key_cycle_details` 字段 → tier attempt 明细不入库。
- 修：handlers 成功路径补 `metrics["key_cycle_details"] = result.key_cycle_attempts`（和失败路径一致）。
- 数据支撑：本轮 rebuild 后 hm_tier_attempts 仍 0，hm_requests 已恢复。

### 2. [cc2] 40005 NV last-resort 迁 NVCF pexec（R41-1 遗留，高价值）
- R41-1 数据：cc2 40005 NV last-resort 24h 触发 43 次，成功仅 8（18.6%），37 次在 attempt 4 (~102s) BUDGET-EXHAUSTED 失败。front 3 keys 全 non-429 timeout → NV integrate API degraded。
- hm40006 已用 NVCF pexec（ACTIVE functions, SOCKS5），40005 仍走 integrate API。
- 迁移后 NV 经 pexec 直连，绕过 degraded integrate API，预期成功率/延迟双改善。
- 范围大（cc-proxy upstream + config），需单独一轮。

### 3. [cc2] HM 延迟优化（数据待 DB 攒够样本后）
- 本轮 6/24 样本 p50=77.9s（含 fallback 累积），单 tier 成功 glm5.1~8-28s vs kimi~1.7s。
- R41-1 已把 NV_TIER_TIMEOUT_BUDGET_S 90→45 限制失败侧耗时。
- 待 DB 攒 24h 样本后用 `hm_log_query.sh tier-health` 复核各 tier p50/p90，决定是否调 tier 顺序（kimi 是否该提前）。

### 4. [cc2] nv_hm_41101 孤儿容器（低优）
- R38.13 声明移除 LiteLLM NV HM 容器，但 cc2 上 nv_hm_41101 仍 Up 3h+（orphan）。
- hm40006 已不路由到它（R38.12 起直连 NVCF pexec）。
- 清理：`docker rm -f nv_hm_41101` + 从 docker-compose.yml 删除 service 定义（若还在）。

## 接力规则（交替优化框架）
- cc1 优化 cc2，cc2 优化 cc1，交替进行，绝不改自己本地。
- 每轮少改动多轮积累，所有改动有日志/文档支撑，稳定优先。
- 评判：更少报错、更快请求、超低延迟。
- 部署验证必须 `docker exec ... grep` + `docker inspect Created`，不能只看 git log（code≠deployment）。
