# Round R35.11 (第4轮) — 2026-06-22 18:00 CST

## R35.10 部署后验证数据（2.5h, 15:36-18:00 CST）

### 40005 (cc-proxy, EXPERIMENT, 395 entries)

| 指标 | R35.10 部署后 | R35.9前(06-22 early) | 变化 |
|------|-------------|---------------------|------|
| 总请求 | 395 | ~623 (pre-rebuild) | — |
| 200率 | 98.5% (389/395) | ~99% | 稳定 |
| FR capture (200 streaming) | 100.0% (387/387) | — | cc-proxy一直正常 |
| 429 cycling率 (200) | 42.9% (167/389) | ~38-49% | 在预期范围 |
| ABORT率 | 1.5% (6/395) | — | 低 |
| Avg TTFB | 8108ms | 9309ms(pre-rebuild) | ↓12% |
| Avg Duration | 13721ms | — | 正常 |

429 cycling 分布: 1-key=59, 2-key=43, 3-key=38, 4-key=16, 5-key=6, 6-key=5
高RPM(>=3/min) → cycling 44.5%, 低RPM → cycling 32.1%
6个ABORT分布在v5,v6,v1,v3,v8,v8 — 非系统性

### 40003 (passthrough, 11 entries, 低流量)

| 指标 | R35.10 部署后 | R35.10前(旧chunk parsing) | 变化 |
|------|-------------|--------------------------|------|
| 总请求 | 11 | 229 | 流量少 |
| 200率 | 100% (11/11) | 98.2% (225/229) | ↑无ABORT |
| FR capture (200 streaming) | **87.5%** (7/8) | **7.2%** (16/222) | ✅ 12x改善 |
| 429 cycling率 | 0% | 36.0% | 0 (流量少) |
| MSG-FIX triggers | 2 (proxy.log) | 0 | ✅ 功能工作 |

唯一 KEY_MISSING 条目: output_tokens=0, duration=22086ms — 可能是 ModelScope 边缘情况（模型返回空content+reasoning_content，无finish_reason chunk）

### 40001 (MIRROR/STABLE): 1 entry — dispatcher 极少路由到它

### ⚡ 重大发现：NV glm-5.1 API 恢复工作！

**测试结果** (5 sequential non-streaming requests via mihomo ♻️US-NV 7894):
- 成功率: 5/5 (100%) — **首次确认自 R35.3 禁用后 NV glm-5.1 API 可用！**
- 延迟: 2199ms, 3395ms, 7566ms, 7880ms, 3579ms (avg ~5s)
- Streaming: 正常工作, finish_reason="stop" in last chunk
- thinking_budget: 仍然 400 Unsupported (proxy 需 strip)

**风险评估**:
- NV 可能是临时��复（之前完全不可用数月）
- NV 延迟不稳定 (2-8s, burst queue 效果)
- NV 提供独立 quota 源（如启用可减少 MS 429 cycling）
- re-enablement 需要 HTTPS CONNECT tunnel + thinking_budget strip（已有代码，只是 NV_NUM_KEYS=0 禁用了）

---

## Action: 本轮无需修改 ✅

**不做的事**（稳定优先，无数据支撑不改）:
- ❌ 不重新启用 NV: 仅测试了5次请求，NV 可能临时恢复，需 24-48h 稳定性监控
- ❌ 不降低 throttle 到 1.0: 429 cycling率42.9%仍是 RPM burst根因，降低间隔加剧burst
- ❌ 不修改 MSG-FIX metrics 记录: 仅2次触发，非紧急
- ❌ 不修改任何参数: 系统运行稳定

---

## 参数现状 (R35.11)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL ports) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False | PROXY_TIMEOUT import in stream.py | dispatcher close_connection | passthrough SSE buffer-based parsing (R35.9, VERIFIED FR 7.2%→87.5%) | cc-proxy buffer-based parsing (100% FR) | MSG-FIX (R35.10, VERIFIED 2 triggers) | NV glm-5.1 API now WORKING (not yet re-enabled)

## 下轮待办 (R35.12)
- 🔥 **NV 稳定性持续监控**：如 NV glm-5.1 API 持续可用 >48h，可考虑重新启用 NV_NUM_KEYS=2（先在 40005 EXPERIMENT 上测试）
- **40003 finish_reason 更多数据**：当前仅 8 条 streaming entries，需更多流量数据验证 87.5% → >95%
- **MSG-FIX metrics 记录**：添加 `msg_fix_appended` field 到 metrics dict（低优先级）
- **NV re-enablement 计划**（如果稳定性确认）：
  1. 40005 NV_NUM_KEYS=2 (EXPERIMENT) — 先在蓝绿实验端测试
  2. 观察 40005 vs 40001 429 cycling 率对比
  3. 如果 NV 减少 429 cycling → 版本提升到 40001
  4. 如果 NV 不稳定 → 回滚 NV_NUM_KEYS=0
