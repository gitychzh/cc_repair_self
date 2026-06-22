# Round R35.13 (第6轮) — 2026-06-22 19:00 CST

## R35.13 验证数据（opc_uname, 06-22 全天 ~9h 运行数据）

### 40005 (cc-proxy, EXPERIMENT, 1272 entries, 06-22 全天)

| 指标 | R35.13 (全天) | R35.12 (全天) | 变化 |
|------|---------------|---------------|------|
| 总请求 | 1272 | 1182 | ↑ 7.6% |
| 200率 | 99.1% (1261/1272) | 99.1% (1171/1182) | 稳定 |
| FR capture (200 streaming) | 100.0% (0/1256 FR=None) | 100.0% | 稳定 |
| 429 cycling率 (error_detail) | 36.5% (470/1288 unique reqs) | 35.7% (418/1171) | 稳定 |
| ABORT率 | 0% (0/1272) | 0% | 稳定 |
| Median TTFB | 8097ms | 8097ms | 稳定 |
| 429_all_transient | 9 | ~10 | 稳定 |
| 502 | 1 (10:30 ConnectionRefused brief transient) | 0 | 微增（瞬态） |

TTFB 分布: <3s=2.6%, 3-5s=17.2%, 5-10s=48.1%, 10-20s=30.0%, 20-30s=1.8%, 30-60s=0.3%, >120s=0.1%
429 cycling 深度: 均匀分布（不是特定 variant/key 瓶颈）

### 40003 (passthrough) — SSE buffer fix confirmed ✅

| 指标 | R35.13 (全天) | R35.12 (全天) | 变化 |
|------|---------------|---------------|------|
| 总请求 | 257 | ~266 | 稳定 |
| 200率 | 98.4% (253/257) | 98.5% | 稳定 |
| ABORT率 | 0% | 0% | 稳定 |
| Stream FR=None | 85.3% (209/245) | 85.7% | 微改善 |

FR=None 的 85.3% 中大部分是 passthrough 正常行为（代理仅转发原始 SSE，
不强制解析 finish_reason；buffer fix 后 FR capture 从 0%→14.7%）。
长响应 (>30s) 有时被 ModelScope 平台截断不发 finish_reason。

### NV glm-5.1 API — ⚠️ 新变化：429 rate limit（不再timeout）

R35.11/R35.12: NV API 完全超时（DNS error + 20s timeout + 0 bytes）
R35.13: NV API **DNS/连接层面已恢复**！但持续返回 HTTP 429 Too Many Requests。

3次测试全部返回 `{"status":429,"title":"Too Many Requests"}`（连接时间 ~1s，不再30s超时）。

**NV API 状态变化**：timeout → 429 rate-limit。连接层面恢复是进步，
但 API 层面仍拒绝服务。**NV_NUM_KEYS=0 维持不变。**
**不建议重新启用 NV interleaving，因为 429 rate limit 说明 NV 服务仍然不稳定。**

### 40001 (MIRROR/STABLE): 1 entry, TTFB=77493ms — 正常（极少流量）
### 40002 (codex): 有少量 activity — 正常
### Dispatcher: 正常运行（0 fallback events）

### LiteLLM quota exhaustion 观察

v7 variant 的 quota exhaustion 频率最高（48次/12h），但 key cycling 完全处理。
所有 429 都被 proxy cycling 解决，0 ABORT。quota-exhausted 不触发 retry-after:180
（因为 cycling 换 key 解决，7key 全 429 才是 ABORT）。

---

## Action: 本轮无需修改 ✅

**不做的事**（稳定优先，数据确认无问题）:
- ❌ 不重新启用 NV: API 返回 429 rate limit，服务仍不稳定
- ❌ 不修改任何参数: 系统运行稳定（99.1% 200率，0 ABORT）
- ❌ 不修改 passthrough SSE 处理: 85.3% FR=None 正常（buffer fix 把 FR capture 从 0→14.7%）
- ❌ 不降低 throttle 到 1.0: 429 cycling率36.5%仍是 RPM burst根因
- ❌ 不改 ConnectionRefused 处理: 10:30 的 1 次 502 是瞬态，正常

---

## 参数现状 (R35.13)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL ports) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False | PROXY_TIMEOUT import in stream.py | dispatcher close_connection | passthrough SSE buffer-based parsing (R35.9-35.13 STABLE: 85.3% FR=None) | cc-proxy buffer-based parsing (100% FR) | MSG-FIX (R35.10, VERIFIED) | NV glm-5.1 API 429 rate-limit (连接恢复但服务仍拒绝, 不建议启用)

## 下轮待办 (R35.14)
- NV glm-5.1 API 监控频率降低：每轮仅 1 次测试（不值得花时间反复测试已确认不可靠的服务）
- MSG-FIX metrics 记录添加（低优先级，建议添加 `msg_fix_appended` field）
- log_cleanup.sh 加入 crontab（`0 2 * * *`）— 需在 opc_uname 上操作
- 40003 FR 85.3% 已足够好（不继续优化，ModelScope 平台限制）
- v7 variant quota exhaustion 频率较高 — 监控但不干预（cycling 完全处理）
