# Round R35.2 — 2026-06-21

## ⏱ 数据时间节点
- ANALYZED_UNTIL: 2026-06-21T03:25:00  # Round 3 从此之后截取
- 下轮命令: `tail -N /opt/cc-infra/logs/proxy40005/proxy.2026-06-21.log`（N=最新行数）

## Round 2 改动（R35.1续）
- **NV_NUM_KEYS: 2→0** (40005 only, 纯MS模式)
  - NV API 完全不可用（60s无响应, 8.7%成功率, 55.8%超时率）
  - NV_FALLTHROUGH→MS 每次浪费 ~40s（2 keys × 20s timeout）
  - 纯MS avg TTFB=6.4s vs NV-involved avg=40.9s — 禁用NV节省34.5s/请求
  - 40001 保持 NV_NUM_KEYS=5 作为蓝绿对比基线

## Round 3 改动（R35.2）
- **MIN_OUTBOUND_INTERVAL_S: 2.0→1.5** (40005 only)
  - 数据支撑: 5小时稳定纯MS运行, 0 ABORT, avg TTFB 8.5s, 62% 0-cycle成功, 极低429率
  - 预期吞吐量提升 ~25%（每请求节省0.5s间隔）
  - 实测: 15请求全部200 OK, TTFB 1.2-4.6s, 0 429, 0 ABORT, 0 fallback
  - burst测试: 10请求连续1.5s间隔, 全部200, avg TTFB 1.5s, key cycles 0.25
  - 风险: 若429率上升需立即回滚到2.0

## 部署后实测（R35.2）
- 40005 单请求: 200 OK, TTFB 1.85s
- 40005 5请求测试: 全部200, TTFB 1.8-4.6s
- 40005 burst 10请求 (1.5s间隔): 全部200, TTFB 1.2-2.4s, avg key cycles=0.25
- Dispatcher :40000 → :40005 链路: 200 OK, TTFB 1.82s, 无fallback
- 40001 baseline: 200 OK, TTFB 1.85s
- 5分钟监控: 63请求全200, 0 error, avg cycles=0.25, 51次0-cycle (80%)
- Dispatcher: 0 recent fallback (仅rebuild期间的历史fallback)

## Round 4 待办
- 持续监控 R35.2 的429率变化（1.5s间隔 vs 2.0s间隔）
- 若429率稳定 → 考虑进一步降低到1.0（更激进，风险更大）
- 若429率上升 → 立即回滚到2.0
- 对比 40005(MIN_INTERVAL=1.5) vs 40001(MIN_INTERVAL=2.0) 的 metrics
- NV API 可用性复查（若恢复 → 重新评估 NV_NUM_KEYS）
- proxy日志无rotation机制 → 需考虑log cleanup策略

## 参数现状（40005）
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s | NV_NUM_KEYS=0 | transient-retry-after=5

## 参数现状（40001 baseline）
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=2.0s | NV_NUM_KEYS=5 | MS_NV_TOTAL_SLOTS=12 | transient-retry-after=5
