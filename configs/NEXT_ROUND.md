# Round R35.9 (第3轮) — 2026-06-22

## R35.8 部署后验证数据（2.5h，10:04-13:26 UTC）

### 40003 (passthrough, throttle=1.5, POST-DEPLOY)

| 指标 | R35.8 部署后 | R35.8前(06-21, throttle=2.0) | 变化 |
|------|-------------|---------------------------|------|
| 总请求 | 223 | 274 | - |
| 200率 | 98.2% (219/223) | 100% (274/274) | ⚠️ 出现4个429终态ABORT |
| 429 cycling率 (key_cycle_429s>0) | 36.1% | 35.8% | 基本不变 |
| 总429 key-cycles | 159 | 236 | ↓减少33% |
| Avg TTFB (200) | 10273ms | 8386ms | ↑+22% |
| Avg Duration (200) | 15099ms | 10466ms | ↑+44% |

**⚠️ TTFB上升原因分析**：
- 06-22 10h UTC：215 req vs 06-21 10h UTC：52 req — 请求量4倍
- 06-22 10h UTC 40005：136 req vs 06-21 10h UTC：19 req — CC请求量7倍
- 合计RPM: 2.6 RPM（3.7%容量利用率），但burst模式仍然导致429
- TTFB上升是流量增加导致的，不是throttle=1.5本身恶化

**结论**：throttle 1.5 vs 2.0 对429 cycling率影响不大（36% vs 36%），根因仍是ModelScope RPM burst throttle。降低throttle到1.0可能进一步加剧burst，不建议此时尝试。

### 40005 (cc-proxy, throttle=1.5)

| 指标 | 06-22 POST-DEPLOY | 
|------|-------------------|
| 总请求 | 311 (280+31) |
| 200率 | 99.6% (279/280) |
| 429 cycling率 | 38.6% |
| Avg TTFB | 10470ms |
| Avg Duration | 14385ms |
| finish_reason分布 | tool_calls=274, stop=5, None=1 |

**40005 finish_reason只有1条None**（vs 40003 206条None）— cc-proxy buffer-based解析工作正常。

### 关键发现：40003 finish_reason SSE解析bug（94% None）

**根因**：passthrough `_stream_openai_passthrough` 使用 chunk-based line parsing（每次read(8192)后split("\n")），SSE data行可跨越8KB chunk边界 → 被拆分的行无法被完整解析 → finish_reason丢失。

**对比**：cc-proxy `stream_to_anth` 使用 buffer-based parsing（`buffer += chunk.decode()` + `while "\n\n" in buffer`），正确处理SSE跨chunk边界。

**证据**：
- 40003 finish_reason=None: 206/219 (94.1%) 200 streaming entries
- 40005 finish_reason=None: 1/280 (0.4%) 200 streaming entries
- 40003 fr=None 平均 duration=15485ms, fr=nonnull 平均 duration=9519ms（长响应更易跨chunk边界）

### 40005 ConnectionRefusedError 事件

- 10条 upstream_ConnectionRefusedError，全部为 variant_idx=9 (glm5.1v10k*)
- 时间窗口: 10:30-10:31 UTC（约2分钟后恢复）
- 2条 socket_timeout (glm5.1v6k3, glm5.1v6k6)

### 40001 (mirror/backup)

- 仅1个请求（77s TTFB, 3次429 cycling）— dispatcher极少路由到它
- 1次 double-failure (40005和40001同时超时)，发生在容器重建期间

---

## Action 1: 修复 40003 SSE finish_reason 解析 bug — ✅ CODE DONE, 待部署

**WHY**: 94% finish_reason=None 使 passthrough proxy 的响应质量监控完全失效。cc-proxy 已用buffer-based解析100%正常，passthrough 也应采用。

**改动**: `passthrough-proxy/gateway/handlers.py` `_stream_openai_passthrough` 方法
- 新增 `sse_buffer = ""` 行级缓冲变量
- `sse_buffer += chunk.decode()` 累积解码文本
- `while "\n" in sse_buffer:` 仅处理完整行
- `sse_buffer.split("\n", 1)` 从缓冲取出完整行
- 流结束时处理缓冲剩余行（finish_reason可能在最后一个SSE事件中）
- passthrough 写 chunk→wfile 的行为不变（纯透传）

**风险**: LOW — 仅影响 metrics 提取，不改变 SSE 透传行为。cc-proxy 已验证此模式。

---

## Action 2: 参数观察 — 🔄 不变更

**数据支撑的结论**:
- throttle 1.5 vs 2.0: 429 cycling率基本相同（36.1% vs 35.8%），没有明显改善
- throttle 不宜进一步降低到 1.0 — burst throttle根因不变，降低间隔会加剧burst
- 40001/40005镜像保持一致 — 蓝绿架构健康
- NV glm-5.1仍不可用 — 无数据支撑重启NV

**不做的事**:
- ❌ throttle 1.5→1.0: 429 cycling率无改善证据，降低间隔可能加剧burst
- ❌ NV 重启: NV API仍不可用，无数据支撑
- ❌ LiteLLM router策略变更: simple-shuffle vs least-busy需更多数据
- ❌ 大范围参数调整: 稳定优先

---

## 参数现状 (R35.9)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL ports) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False | PROXY_TIMEOUT import in stream.py | dispatcher close_connection | passthrough SSE buffer-based parsing (R35.9) | cc-proxy buffer-based parsing (正常)

## 下轮待办 (R35.10)
- 验证 R35.9 SSE buffer fix 部署后 40003 finish_reason 分布（预期 None<5%）
- 继续监控 throttle=1.5 效果（需更多同流量时段对比数据）
- 如finish_reason修复成功，可开始关注 passthrough proxy 的其他指标（output_tokens分布、error_type分布）
