# Round R35.2 — 2026-06-21

## R35.2 Round 2 数据分析总结

### 数据验证结果 (03:20-03:30 CST, MIN_OUTBOUND_INTERVAL_S=1.5生效后)
- 76个请求，100%成功率，0 ABORT
- avg TTFB 5.0s（2.0s间隔下10.0s，改善2倍）
- 429 cycling率 30%（2.0s间隔下49%，下降19%）
- 92%请求<10s，0%>30s
- 纯MS无cycling TTFB 3.5s

### R35.2 Round 2 变更
1. **40001同步40005配置**: NV_NUM_KEYS 5→0, MIN_OUTBOUND_INTERVAL_S 2.0→1.5
   - 蓝绿容器现在完全一致，fallback完全无损
   - 40001之前作为fallback时有NV超时拖慢(avg TTFB 38s→94s for NV-involved)

2. **NV_KEY3-5移除**: 40001只保留NV_KEY1-2（与40005一致）

### 不做的变更及原因
- **MIN_OUTBOUND_INTERVAL_S 1.5→1.0**: 429 cycling率仍有30%，更紧间隔风险高
- **UPSTREAM_TIMEOUT 60→30**: MS最长14.3s(LiteLLM duration)，30s安全但收益有限（NV有独立NV_TIMEOUT=20s）
- **NV_TIMEOUT 20→15**: NV成功max=17s有2个>15s(1%)，当前NV已禁用无收益

## 累计优化效果

| 指标 | 原始(NV=5, throttle=2.0) | R35.1(NV=0, throttle=2.0) | R35.2(throttle=1.5, 40001 synced) |
|------|--------------------------|---------------------------|-----------------------------------|
| avg TTFB | ~60s(NV拖慢) | 10.0s | 5.0s |
| 429 cycling率 | N/A | 49% | 30% |
| success rate | 100% | 100% | 100% |
| empty output | 12.1% | 0% | 0% |
| fallback quality | NV拖慢38-94s | 40001仍有NV | 无损(纯MS mirror) |

## Round 3 待办
- 持续监控1.5s interval稳定性（已验证76请求/30分钟）
- 如果1.5s持续稳定>2h，可考虑1.0s（429率需降到<20%）
- 关注NV glm-5.1 API恢复情况（如果恢复可重新启用NV interleaving）
- 关注MS quota消耗趋势（ms_requests_remaining跟踪）
- proxy代码优化：cycling内throttle可考虑降低（当前全局共享throttle，cycling内也等1.5s）

## 参数现状 (40001=40005 mirror)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS)
