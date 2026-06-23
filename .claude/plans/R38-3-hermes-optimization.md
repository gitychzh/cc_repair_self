# R38.3: Hermes 模型链路优化计划

## 数据分析结果

### 请求分布（6h 日志，62次请求）
| Tier | 请求数 | 占比 | 成功率 | 平均延迟 | 原因 |
|------|--------|------|--------|----------|------|
| kimi_hm | 58 | 93.5% | 100% (1×500 cycle→k5 success) | 4.5s | Hermes config.default=kimi_hm |
| glm5.1_hm | 2 | 3.2% | 100% | 4.6s | 仅测试请求 |
| deepseek_hm | 2 | 3.2% | **0%** (全部 ABORT) | N/A | deepseek-v4-pro NV API 完全不可用 |

### 关键发现
1. **Hermes 发送的是 kimi_hm 而不是 glm5.1_hm**：`~/.hermes/config.yaml` 中 `model.default: kimi_hm`，导致 93.5% 请求走 kimi，偏离用户期望
2. **FALLBACK 机制从未真正触发**：0 次 glm5.1→kimi fallback（因为从未用过 glm5.1），0 次 kimi→deepseek fallback（kimi 基本没问题）
3. **deepseek-v4-pro 完全损坏**：2 次请求，5 key 全 timeout/500，浪费 271s+300s = **571s = 9.5 分钟**
4. **deepseek-v4-flash 可用**：NV API 实测 1.4-4.3s 响应，支持 tool calling，建议替换
5. **LiteLLM 连接错误**：5 个容器都有间歇性 SSL 连接错误（mihomo proxy 暂态问题），但 cycling 会恢复

### 延迟分布（kimi_hm，58次）
| 区间 | 请求数 | 占比 |
|------|--------|------|
| 0-3s | 21 | 36.2% |
| 3-5s | 26 | 44.8% |
| 5-10s | 9 | 15.5% |
| 10-30s | 1 | 1.7% |
| 30-60s | 1 | 1.7% |

### 待优化问题及优先级

#### P0: Hermes 默认模型 kcal→glm5.1
- **问题**：Hermes config.default=kimi_hm，不是用户想要的 glm5.1_hm
- **数据证据**：58/62 请求走 kimi_hm（93.5%）
- **方案**：修改 `~/.hermes/config.yaml` 中 `model.default` 和 `providers.litellm-nv-hm.default_model` 从 `kimi_hm` → `glm5.1_hm`
- **影响**：所有后续请求默认走 glm5.1，kimi 仅在 fallback 时使用

#### P1: deepseek-v4-pro → deepseek-v4-flash
- **问题**：deepseek-v4-pro 在 NV API 完全不可用（0% 成功率，2/2 全 ABORT）
- **数据证据**：2 次请求浪费 571s；直连 NV API 30s timeout 0 bytes
- **方案**：LiteLLM 5 个 config-k*.yaml 中 `deepseek-ai/deepseek-v4-pro` → `deepseek-ai/deepseek-v4-flash`
- **验证**：deepseek-v4-flash 实测可用（1.4s short prompt, 4.3s long prompt, tool calling OK）

#### P2: 缩短超时（减少失败浪费的时间）
- **问题**：
  - PROXY_TIMEOUT=300s → deepseek tier 浪费 271-300s
  - LiteLLM request_timeout=60s → 但 NV API p95 延迟仅 9.6s
  - UPSTREAM_TIMEOUT=60s → hm-proxy→LiteLLM HTTP 连接超时
- **数据证据**：成功请求 p95=9.6s, max=36.3s; 失败请求浪费 60s×5keys×3tiers
- **方案**：
  - `PROXY_TIMEOUT`: 300 → **120s**（3 tiers × 5 keys × 8s per key 远低于 120s）
  - `UPSTREAM_TIMEOUT`: 60 → **30s**（hm-proxy→LiteLLM 连接超时，成功请求远低于 30s）
  - LiteLLM config `timeout`: 60 → **30s**，`request_timeout`: 60 → **30s**

#### P3: hm-proxy UPSTREAM_TIMEOUT sock.settimeout (R36.2 核心修复)
- **问题**：http.client.HTTPConnection.timeout 只控 connect 不控 read（R36.2 核心教训）
- **数据证据**：CLAUDE.md 记录 "http.client timeout只控connect不控read" — 对 NV 请求尤其重要
- **方案**：在 `_try_tier_keys()` 中 `conn.request()` 后加 `conn.sock.settimeout(UPSTREAM_TIMEOUT)` 确保读超时生效
- **注意**：cc-proxy 已有此修复，hm-proxy 需要同步

## 修改清单

### 1. `~/.hermes/config.yaml`（Hermes 配置，非仓库文件）
```yaml
model:
  default: glm5.1_hm          # was: kimi_hm
providers:
  litellm-nv-hm:
    default_model: glm5.1_hm   # was: kimi_hm
```

### 2. `configs/litellm-nv-hm/config-k1~k5.yaml`（5个文件）
```yaml
# Line 29: deepseek-ai/deepseek-v4-pro → deepseek-ai/deepseek-v4-flash
- model_name: nvdeepseek_k1
  litellm_params:
    model: openai/deepseek-ai/deepseek-v4-flash  # was: deepseek-v4-pro
    timeout: 30                                    # was: 60
# 同样 timeout: 60→30 for all three models in each config
```

### 3. `configs/proxy/hm-proxy/gateway/config.py`
```python
NV_MODEL_IDS = {
    "deepseek_hm": "deepseek-ai/deepseek-v4-flash",  # was: deepseek-v4-pro
}
# context window: deepseek-v4-flash 131072 (same as v4-pro)
```

### 4. `configs/proxy/hm-proxy/gateway/upstream.py`
- 添加 `conn.sock.settimeout(UPSTREAM_TIMEOUT)` 修复读超时
- 在 `_try_tier_keys()` 中 `conn.request()` + `conn.getresponse()` 后设置

### 5. `configs/docker-compose.yml`
- hm40006: `PROXY_TIMEOUT: "120"` (was: "300"), `UPSTREAM_TIMEOUT: "30"` (was: "60")

### 6. 文档更新
- CLAUDE.md: deepseek_hm model ID 更新, timeout 更新
- DEPLOY_STATUS.md: R38.3 changes

## 不修改的（有原因）

1. **不增加 NV key 数量**：5 key 已足够覆盖 kimi_hm 100% 成功率
2. **不减少 fallback tiers**：3 tier fallback（glm5.1→kimi→deepseek）保留，deepseek-v4-flash 已验证可用
3. **不改 MIN_OUTBOUND_INTERVAL_S=1.5**：当前 429 率极低（6h 内 0 个 429 在 LiteLLM 层），无理由调整
4. **不改 MS fallback (40003)**：Hermes 级别的 provider fallback 已独立运行

## 预期效果
- 请求默认走 glm5.1_hm（用户期望的模型）
- deepseek fallback 从 0% → ~95%+ 成功率
- 失败请求超时从 ~271-300s → ~30-60s（省 4-5 分钟/次）
- Fallback 链路可验证生效
