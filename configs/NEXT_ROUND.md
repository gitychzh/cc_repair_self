# Round R35.8+ (第2轮) — 2026-06-22

## 重大发现：R35.7 和 R35.8 的所有改动均未部署

### 证据

**容器运行代码与 git repo 严重不一致**：

| 文件 | git repo | /opt/cc-infra | 容器内 | 缺失改动 |
|------|----------|---------------|--------|----------|
| passthrough/handlers.py | 852 | 845 | 845 | R35.8 finish_reason stream提取 (7行) |
| passthrough/upstream.py | 1044 | 1041 | - | NV error classification (+3) |
| passthrough/error_mapping.py | 393 | 386 | - | NV error types (+7) |
| cc-proxy/upstream.py | 1043 | 1040 | - | NV error classification (+3) |
| cc-proxy/error_mapping.py | 168 | 167 | - | NV error type (+1) |
| codex-proxy/upstream.py | 691 | 688 | - | NV error classification (+3) |
| codex-proxy/error_mapping.py | 387 | 386 | - | NV error type (+1) |
| dispatcher/gateway_main.py | 154 | 148 | 148 | R35.7 close_connection fix (+6行) |
| docker-compose.yml 40003 | throttle=1.5 | throttle=2.0 | env=2.0 | R35.8 throttle变更 |

**具体缺失的 fix**：
1. R35.7: dispatcher close_connection on error (6行, HIGH priority)
2. R35.7: PROXY_TIMEOUT NameError import (stream.py)
3. R35.7: Operator precedence fix
4. R35.7: key_idx KeyError fix
5. R35.7: NV error type classification (3 proxy × upstream+error_mapping)
6. R35.8: 40003 throttle 2.0→1.5
7. R35.8: passthrough stream finish_reason extraction

**后果**：
- 40003 throttle=2.0 → 06-22 数据：429 cycling=24%, TTFB=8.2s, 高峰(12h) cycling=67%
- 40003 finish_reason 全部 null → 无法监控响应质量
- dispatcher close_connection 缺失 → 客户端可能复用死亡连接
- NV error classification 缺失 → 未来NV启用时可能误分类错误

### 40003 06-22 数据 (throttle=2.0, 仍然旧配置)

| 时段 | Requests | 429 cycling% | Avg TTFB | ABORT | Success% |
|------|----------|-------------|----------|-------|----------|
| 全天 | 50 | 24.0% | 8.2s | 0 | 100% |
| 8-11h | 24 | 25.0% | 8.5s | 0 | 100% |
| 12h(高峰) | 6 | **66.7%** | 10.7s | 0 | 100% |
| 0-7h(低峰) | 18 | 11.1% | 7.2s | 0 | 100% |

对比 40005(throttle=1.5) 06-21 8-11h：cycling=6.7%, TTFB=2.5s

### 40003 06-22 error_detail (15 entries)
全部为 `429_rate_limit_key_cycle_attempt` — 无 ABORT、无 500/502、无 connection error。
其中 3 条多 key cycling（同一请求换 2+个 key）。

### 40005 数据
06-22 无新数据（CC未活跃）。06-21 全天305次：200=302, 429=2, cycling=39.7%。

---

## 优化计划（第2轮）— 执行状态

### Action 1: 紧急部署 sync + rebuild — ✅ COMPLETED (2026-06-22 13:05 CST)

**WHY**: R35.7/R35.8 的全部改动未部署，这是 R35.7 教训的第三次重复。容器运行着已知有 bug 的旧代码。

**执行记录**:
1. ✅ opc_uname: git pull (6 commits from R35.7+R35.8)
2. ✅ opc_uname: bash scripts/sync_config.sh (docker-compose.yml + 11 proxy source files updated)
3. ✅ opc_uname: docker compose up -d --build --force-recreate (all 5 containers rebuilt, all healthy)
4. ✅ 验证:
   - 40003 throttle: MIN_OUTBOUND_INTERVAL_S=1.5 ✅ (was 2.0)
   - 40003 finish_reason fix: R35.8 marker present ✅ (was absent)
   - 40005 throttle: MIN_OUTBOUND_INTERVAL_S=1.5 ✅
   - 40001 throttle: MIN_OUTBOUND_INTERVAL_S=1.5 ✅
   - dispatcher close_connection: 3 occurrences ✅ (was 0)
   - All health endpoints: ✅
   - curl smoke test 40005 + 40003: ✅ (200 response)
   - 40003 latest metrics: finish_reason=length (non-null!) ✅ (was all null)
bash ~/cc_ps/cc_repair_self/scripts/sync_config.sh

# 2. rebuild 所有受影响容器
cd /opt/cc-infra && docker compose up -d --build --force-recreate \
  auth_to_api_40000 auth_to_api_40001 auth_to_api_40002 auth_to_api_40003 auth_to_api_40005

# 3. 验证每个 fix 是否真正部署
docker exec auth_to_api_40003 cat /app/gateway/handlers.py | grep -c 'R35.8'  # 应>0
docker exec auth_to_api_40003 env | grep MIN_OUTBOUND  # 应=1.5
docker exec auth_to_api_40000 cat /app/dispatcher.py | grep close_connection  # 应有
docker exec auth_to_api_40005 env | grep MIN_OUTBOUND  # 应=1.5

# 4. smoke test
curl -s http://127.0.0.1:40003/v1/chat/completions ...
curl -s http://127.0.0.1:40005/v1/messages ...
curl -s http://127.0.0.1:40000/health
```

**风险**: LOW — 所有改动已在 R35.7/R35.8 中验证过代码正确性，只是没部署。
**影响范围**: 所有 5 个 proxy 容器 + dispatcher
**预计改善**: 40003 throttle 从 2.0→1.5 → 429 cycling 降 ~15%, TTFB 降 ~2-3s

### Action 2: 部署后数据观察 — 🔄 进行中 (需要 30min+ 数据积累)

部署完成后，等 ~30min 让 OpenClaw 产生足够请求，然后采集数据验证：
- 40003 throttle=1.5 效果：429 cycling 率是否下降
- 40003 finish_reason 分布：应看到 stop/length/tool_calls 而非全部 null
- dispatcher close_connection：fallback 行为是否改善

初始验证结果（13:05-13:07, 3 requests POST-rebuild）:
- 40003: cycling=1(33.3%), TTFB=9.7s, fr_null=2, fr_nonnull=1
- 需等更多数据才能判断

---

## 下轮待办 (R35.9)
- 收集 1-2h 数据后对比 throttle=1.5 vs 旧 throttle=2.0 的效果
- finish_reason 分布分析（应看到 length/stop/tool_calls，不再是全 null）
- throttle 1.5→1.0 测试需有人值守（TUNE_RULES 要求 429<5%）
- 检查 LiteLLM router_strategy 是否可优化（当前 simple-shuffle）
- 防止 sync_config遗漏：考虑在 CLAUDE.md 或脚本中添加自动检查

**不做的事（本轮）**:
- ❌ throttle 1.5→1.0: 需先验证 1.5 部署效果，且需有人值守（TUNE_RULES 要求 429<5%）
- ❌ NV 重启: NV glm-5.1 仍不可用，无数据支撑
- ❌ LiteLLM router 策略变更: simple-shuffle vs least-busy 需更多数据对比
- ❌ 大范围参数调整: 稳定优先，每轮不大改

---

## 参数现状 (✅ 部署已确认)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL ports, 部署后生效) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False | PROXY_TIMEOUT import in stream.py | dispatcher close_connection | passthrough finish_reason extraction

## 下轮待办 (R35.9)
- 监控部署后 40003 throttle=1.5 + finish_reason 提取的实际效果
- 收集 1-2 小时数据后评估 throttle 是否可以进一步降低 (1.5→1.0)
- 检查是否可以简化 sync_config.sh 流程防止再次遗漏
