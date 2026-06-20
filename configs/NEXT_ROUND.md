# Round R35.1 — 2026-06-21

## ⏱ 数据时间节点
- ANALYZED_UNTIL: 2026-06-21T02:55:39  # Round 2 从此之后截取
- 下轮命令: `tail -N /opt/cc-infra/logs/proxy40005/proxy.2026-06-21.log`（N=最新行数）

## Round 1 改动（R35.1）
1. **NV_NUM_KEYS: 5→2** (40005 only, 40001 stays 5 for baseline)
   - MS_NV_TOTAL_SLOTS: 12→9 (7MS+2NV=78%MS+22%NV vs 旧 7+5=58%MS+42%NV)
   - 数据支撑: NV成功率8.7%, 超时55.8%, NV-involved TTFB 165s vs MS 8.8s
2. **host.docker.internal DNS fix** (extra_hosts for 40001/40005/40003)
   - Linux Docker 不解析 host.docker.internal → NV 调用全 gaierror
   - 修复后 NV_FALLTHROUGH→MS 从 ~5s（之前165s）
   - NV API 当前完全不可用（60s无响应），但 gaierror 快速失败

## 部署后实测
- MS-only TTFB: 1.3-1.6s (正常)
- NV FALLTHROUGH→MS: 5.4-5.8s (从165s降到~5s，33倍改善)
- 无 dispatcher fallback
- rr_counter 959→1029（测试期间70次请求）

## Round 2 待办
- 收集更多生产数据验证 R35.1 效果
- 考虑 MIN_OUTBOUND_INTERVAL_S 2.0→1.5（增加吞吐量）
- 如果 NV 持续不可用 → 考虑 NV_NUM_KEYS=0（纯MS模式）
- 对比 40001(NV=5) vs 40005(NV=2) 的 metrics

## 参数现状（40005）
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=2.0s | NV_NUM_KEYS=2 | MS_NV_TOTAL_SLOTS=9 | transient-retry-after=10
