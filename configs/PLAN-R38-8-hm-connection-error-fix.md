# Hermes "Connection error" 修复计划 (R38.8)

## 诊断结果

### 根因链路

**Hermes → 40006 → 连接拒绝 → 3次重试 → 失败 → 没有跨 provider fallback → 卡死**

hm40006 容器不存在（从未在本机部署）。Hermes 尝试连接 40006 端口 3 次，每次 ConnectionRefused，然后报 "API call failed after 3 retries: Connection error"，没有尝试 fallback provider (40003 MS)。

### 4 个问题

| # | 问题 | 影响 | 严重度 |
|---|------|------|--------|
| P1 | **hm40006 容器未运行** | Hermes 所有 NV 请求 ConnectionRefused → 卡死 | **致命** |
| P2 | **hm-proxy 408 超时不 cycling** | LiteLLM 返回 408 时，`should_cycle = resp.status in (429, 500, 502)` 不含 408 → 立即返回错误，不尝试其他 key | **高** |
| P3 | **NV API glm-5.1 当前极慢 (>60s)** | 每次 glm5.1 请求浪费 35s timeout，然后 kimi fallback ~4s，总 ~40s/请求 | **中**（NV API 端问题，需自适应） |
| P4 | **Hermes fallback config default_model 错误** | litellm-local-ms 的 default_model=glm5.1_hm_nv（NV suffix），应为 glm5.1_hm_ms | **低**（40003 实际能处理，但语义错误） |

### 日志证据

- `docker logs hm40006` → 容器不存在（已修复：刚刚手动启动）
- `docker ps` → hm40006 不在列表中
- `docker exec hm40006 env` → 旧容器有 R39 参数 (TIER_TIMEOUT_BUDGET_S=30, UPSTREAM_TIMEOUT=35)，新容器正确 (60, 45)
- `docker logs nv_hm_41101` → LiteLLM 返回 `408 Request Timeout`
- hm-proxy 日志 → glm5.1 tier 只尝试 1 key 即失败，无 HM-TIMEOUT/CYCLE 日志（408 被当作 non-cycling error）
- `curl NV API` → glm-5.1 >120s 超时，kimi/deepseek/gemma 正常 (~5s)

## 修复方案

### Fix 1: 确保 hm40006 容器持续运行 ✅ 已完成
- `docker compose up -d hm40006` → 容器已启动，状态 healthy
- `restart: unless-stopped` 确保 Docker 重启后自动恢复
- **追加**: 在 deploy.sh 中增加 hm40006 健康检查

### Fix 2: hm-proxy 408 超时加入 cycling 错误列表
**文件**: `proxy/hm-proxy/gateway/upstream.py` line 265

```python
# 旧代码:
should_cycle = resp.status in (429, 500, 502)

# 新代码 (R38.8):
should_cycle = resp.status in (429, 500, 502, 408)  # 408=LiteLLM timeout → cycling
```

并添加对应的 cycle_reason：
```python
cycle_reason = "429_nv_rate_limit" if resp.status == 429 else \
               "408_litellm_timeout" if resp.status == 408 else \
               "500_nv_error" if resp.status == 500 else "502_nv_error"
```

**效果**: LiteLLM 408 timeout → cycling to next key → 不浪费整个 tier 的所有 key

### Fix 3: kimi_hm_nv 临时提升为 primary tier（NV glm-5.1 恢复后回退）
**文件**: `proxy/hm-proxy/gateway/config.py` 的 DEFAULT_NV_MODEL

数据证明：glm-5.1 在 NV API 当前极慢 (>60s)，每次请求浪费 35s。
kimi-k2.6 在 NV API 稳定 (~4-5s)，作为 primary 可立即获得响应。

```python
# 临时改为 kimi 优先（NV glm-5.1 恢复后回退）
DEFAULT_NV_MODEL = "kimi_hm_nv"  # R38.8 temp: glm5.1 NV极慢>kimi优先
NV_MODEL_TIERS = ["kimi_hm_nv", "glm5.1_hm_nv", "deepseek_hm_nv"]
```

**注意**: 这是临时调整。当 NV glm-5.1 恢复正常延迟 (<20s)，应回退为 glm5.1 primary。

### Fix 4: Hermes fallback config 修正 default_model
**文件**: `~/.hermes/config.yaml` (本机配置，不进仓库)

```yaml
litellm-local-ms:
  default_model: glm5.1_hm_ms  # 修正: MS endpoint 应使用 _hm_ms suffix
```

### Fix 5: deploy.sh 增加 hm40006 健康检查
**文件**: `scripts/deploy.sh`

```bash
# Test hm40006 (Hermes NV proxy)
echo "  Testing hm40006 (Hermes NV proxy)..."
HM_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 45 -X POST http://127.0.0.1:40006/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer sk-litellm-local" \
    -d '{"model":"kimi_hm_nv","messages":[{"role":"user","content":"test"}],"max_tokens":10}')
echo "  hm40006 HTTP status: ${HM_RESULT}"
```

### Fix 6: docker-compose.yml TIER_TIMEOUT_BUDGET_S 确保一致
当前部署版本已经是正确的 (TIER_TIMEOUT_BUDGET_S=60, UPSTREAM_TIMEOUT=45)。
只需确认 repo 版本也是一致的 → 已一致，无需修改。

## 实施顺序

1. **Fix 2**: hm-proxy 408 cycling fix → rebuild hm40006
2. **Fix 3**: kimi 作为临时 primary → rebuild hm40006 (与 Fix 2 合并)
3. **Fix 4**: Hermes config.yaml → 直接修改
4. **Fix 5**: deploy.sh → push 到仓库
5. **Fix 1**: hm40006 容器已在运行 → rebuild 应用代码变更
6. **验证**: curl 测试 + Hermes 实际请求测试
7. **Push**: 代码变更 push 到仓库

## 验证步骤

```bash
# 1. rebuild hm40006
cd /opt/cc-infra && docker compose up -d --build --force-recreate hm40006

# 2. 测试 hm40006 直接请求（应快速返回，kimi primary）
curl -s -m 15 -X POST http://127.0.0.1:40006/v1/chat/completions \
  -H "Authorization: Bearer sk-litellm-local" \
  -H "Content-Type: application/json" \
  -d '{"model":"kimi_hm_nv","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'

# 3. 测试 glm5.1_hm_nv（应 cycling 408 → next key → 或 fallback kimi）
curl -s -m 45 -X POST http://127.0.0.1:40006/v1/chat/completions \
  -H "Authorization: Bearer sk-litellm-local" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm5.1_hm_nv","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'

# 4. 检查 hm40006 日志应显示 HM-CYCLE(408) 或 HM-TIMEOUT
docker logs hm40006 --tail 20

# 5. Hermes 实际请求测试（等 Hermes 自然触发或手动请求）
```
