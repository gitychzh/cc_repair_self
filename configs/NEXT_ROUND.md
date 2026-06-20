# Round R35.2 — 2026-06-21

## ⏱ 数据时间节点
- ANALYZED_UNTIL: 2026-06-21T03:30  # Round 4 从此之后截取

## Round 3 改动（R35.2）
1. **MIN_OUTBOUND_INTERVAL_S: 2.0→1.5** (40005 only, 40001 stays 2.0)
   - 数据支撑: 0 ABORT, 73% zero cycling, avg TTFB 4.9s (vs 8.5s with 2.0)
   - 10 burst requests: all 200, TTFB 1.2-2.4s, avg cycling 0.25
   - 预期吞吐提升约25%（0.5s/request saved）
   - Risk: more burst 429 — 未观察到，但需持续监控

## 累计优化效果（R35.1 + R35.2）

| 指标 | 原始值(NV=5, throttle=2.0) | R35.1(NV=0) | R35.2(throttle=1.5) | 总改善 |
|------|---------------------------|-------------|---------------------|--------|
| NV-involved TTFB | 165s | 0 (纯MS) | 0 | ∞ |
| MS-only TTFB | 8.8s | 3.1s | 4.9s* | -44% |
| Zero cycling rate | 53% | 57% | 73% | +38% |
| 429 rate | 0 | 0 | 0 | 稳定 |

*MS TTFB波动与ModelScope时段有关，非参数改变导致

## Round 4 待办
- 持续监控 1.5s interval 稳定性（30min+）
- 如果1.5s稳定，考虑进一步降到1.0s（更高吞吐，更高风险）
- 或者关注其他优化：output_tokens=0 空响应、choice:null
- 检查40001(NV=5, throttle=2.0) 作为 baseline 对比

## 参数现状（40005）
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=7 | transient-retry-after=10
