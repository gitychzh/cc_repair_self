# Round R35.8 — 2026-06-22

## R35.8: 40003 Throttle Alignment + Passthrough null_finish Metrics Fix

### 数据分析（R35.7 部署后：06-21 17:40 + 06-22）

| Proxy | Requests | 429 cycling% | Avg TTFB | ABORT | Success% |
|-------|----------|-------------|----------|-------|----------|
| 40005 CC (throttle=1.5s) | 155 | 36% | 6.9s | 0 | 100% |
| 40003 OpenClaw (throttle=2.0s) | 150 | 33% | 10.0s | 3 | 98% |

### 关键发现

1. **跨 proxy RPM 竞争**：82% 的 CC(40005) 请求与 OpenClaw(40003) 请求时间重叠，共享同一 LiteLLM backend。虽然 throttle 限制了单 proxy 出站速率，但两 proxy 请求到达 ModelScope 时仍可能形成 burst。
2. **40003 throttle=2.0 不比 1.5 好**：3 次 ABORT 在 40003，0 次 ABORT 在 40005。更长的 throttle 没减少 429，只增加了 TTFB（10s vs 6.9s）。
3. **Ghost-ABORT 修复生效**：40003 metrics 现在正确记录 ABORT（status=429），不再隐瞒失败。
4. **40003 null_finish**：`_stream_openai_passthrough` 不提取 finish_reason → 99% null。不影响客户端（byte-level passthrough），但影响监控质量。
5. **40003 stale dsv4p rr_counter**：`{"dsv4p": 6, "glm5.1": 474}` 残留。
6. **时间段差异**：10AM cycling=56%（高峰），17:40+ cycling=22%（低峰），符合 metrics-interpretation 中的时段TTFB模式。

### 变更 Action 1: 40003 throttle 2.0→1.5

- **WHY**: 数据证明 throttle=2.0 有 3 ABORT + avg TTFB=10s，而 40005 throttle=1.5 有 0 ABORT + avg TTFB=6.9s。更长 throttle 不减少 ABORT，只增加延迟。
- **文件**: `configs/docker-compose.yml`
- **风险**: LOW — 1.5s 在 40005/40001 已稳定运行 16+ 小时

### 变更 Action 2: Passthrough null_finish metrics 修复

- **WHY**: `_stream_openai_passthrough` 不从 SSE chunk 提取 finish_reason → 99% null → 无法监控响应质量。
- **文件**: `configs/proxy/passthrough-proxy/gateway/handlers.py`
- **修复**: 在 SSE 解析循环中添加 `fr = data.get("choices", [{}])[0].get("finish_reason")` → `metrics["finish_reason"] = fr`
- **风险**: VERY LOW — 只修改 metrics dict，不影响 byte-level passthrough

### 变更 Action 3: 40003 stale dsv4p rr_counter 清理

- `{"dsv4p": 6, "glm5.1": 517}` → `{"glm5.1": 517}`
- 纯 cosmetic，代码只读 glm5.1 key

### 部署验证 (10:40 CST)

- 40003 health: ✅ `{"status":"ok","proxy_role":"passthrough","gateways":{"glm5.1":...},"port":40003}`
- 40003 throttle: ✅ `MIN_OUTBOUND_INTERVAL_S=1.5`
- 40003 finish_reason code: ✅ deployed and working (smoke test showed `finish_reason=length`)
- 40003 rr_counter: ✅ `{"glm5.1": 517}` (dsv4p removed)
- curl 40003 request test: ✅ 200

## 参数现状 (ALL proxies aligned: throttle=1.5s)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL ports) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False(all ports) | PROXY_TIMEOUT import in stream.py(fixed) | dispatcher close_connection(fixed) | finish_reason extraction in passthrough(fixed R35.8)

## 下轮待办
- 监控 40003 throttle=1.5s 效果：429 cycling率是否降到与 40005 相近？TTFB 是否改善？
- 监控 40003 finish_reason 分布（应看到 stop/tool_calls/length 而非 99% null）
- 关注跨 proxy 竞争：CC 和 OpenClaw 同时活跃时 429 是否更严重？
- throttle 1.5→1.0 测试（需有人值守，TUNE_RULES.md 要求 429_rate<5% 才可降低）
- 检查 LiteLLM router_strategy 是否可优化（当前 simple-shuffle，是否换 least-busy 可减少竞争？）
