# Round R35.12 (第5轮) — 2026-06-22 18:30 CST

## R35.12 验证数据（~8h post-last-rebuild, opc_uname）

### 40005 (cc-proxy, EXPERIMENT, 1182 entries, 06-22 全天)

| 指标 | R35.12 (全天) | R35.11 (2.5h) | 变化 |
|------|---------------|----------------|------|
| 总请求 | 1182 | 395 | ↑ 3x |
| 200率 | 99.1% (1171/1182) | 98.5% (389/395) | ↑ |
| FR capture (200 streaming) | 100.0% | 100.0% | 稳定 |
| 429 cycling率 (200) | 35.7% (418/1171) | 42.9% (167/389) | ↓ 7.2% |
| ABORT率 | 0% (0/1182) | 1.5% (6/395) | ↓ 消除 |
| Avg TTFB | 8904ms | 8108ms | ↑ 9.6%* |
| Avg Duration | 14349ms | 13721ms | ↑ 4.6% |

*TTFB上升可能来自更大的request context，非系统退化

429 cycling 分布: 1-key=178, 2-key=115, 3-key=70, 4-key=30, 5-key=14, 6-key=11
cycling 深度均匀分布 → 不是特定 variant/key 的瓶颈

### 40003 (passthrough) — SSE buffer fix re-confirmed ✅

**关键发现**：之前的 86% finish_reason=None 率被旧容器数据污染！

| 时间段 | FR capture率 | 条件 |
|--------|--------------|------|
| 旧容器 (10:xx, chunk-based) | 4/210 = **1.9%** | R35.8 chunk-based parsing |
| 新容器 (15:39+, buffer-based) | 18/21 = **85.7%** | R35.9 buffer-based parsing ✅ |

**3条 post-rebuild FR=None 全是 ModelScope 平台截断**：
1. 22086ms, 0 output_tokens — ModelScope 边缘情况
2. 45170ms, 0 output_tokens — ModelScope 流截断
3. 42806ms, 0 output_tokens — 测试请求（也是截断）

**ModelScope SSE 格式发现**：
- finish_reason chunk 和 usage chunk 是两个独立 SSE data line
- finish_reason line: `data: {"choices":[{"finish_reason":"stop/length","delta":{}]}]}`
- usage line: `data: {"choices":[{"delta":{}],"usage":{...}}}`
- 长响应（>30s）有时被 mid-stream 截断，不发 finish_reason/[DONE]

### NV glm-5.1 API ❌ 再次不可用

R35.11 的 5/5 成功是临时性的。3次 R35.12 测试全部超时（20s, 0 bytes）。
症状与 R35.3 禁用时相同：TLS OK → 0 response bytes → timeout。
**确认 NV glm-5.1 API 不可靠，NV_NUM_KEYS=0 维持不变。**

### 40001 (MIRROR/STABLE): 1 entry — 正常（极少流量）
### 40002 (codex): 0 entries — 正常（Codex 不活跃）
### Dispatcher: 0 fallback events — 正常运行

---

## Action: 本轮无需修改 ✅

**不做的事**（稳定优先，数据确认无问题）:
- ❌ 不重新启用 NV: R35.11 临时恢复已确认失效，不可靠
- ❌ 不修改任何参数: 系统运行稳定（99.1% 200率，0 ABORT）
- ❌ 不修改 passthrough SSE 处理: 85.7% FR capture 是正常水平（14.3% None 来自 ModelScope 平台截断）
- ❌ 不降低 throttle 到 1.0: 429 cycling率35.7%仍是 RPM burst根因

---

## 参数现状 (R35.12)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL ports) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False | PROXY_TIMEOUT import in stream.py | dispatcher close_connection | passthrough SSE buffer-based parsing (R35.9, R35.12 RE-CONFIRMED: 1.9%→85.7%) | cc-proxy buffer-based parsing (100% FR) | MSG-FIX (R35.10, VERIFIED) | NV glm-5.1 API UNAVAILABLE again (R35.11 temporary recovery confirmed transient)

## 下轮待办 (R35.13)
- NV glm-5.1 API 不再监控（已确认不可靠，除非连续72h可用才考虑）
- MSG-FIX metrics 记录添加（低优先级，建议添加 `msg_fix_appended` field）
- log_cleanup.sh 加入 crontab（`0 2 * * *`）
- 40003 FR 85.7% 已足够好（14.3% None 是 ModelScope 平台限制，不可 proxy 层修复）
