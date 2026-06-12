# R21 Plan: 41001 Unified Container + Variant-Level Round-Robin

## 变更概述

用户要求3项变更：
1. 41001容器名 `glm5.1_uni41001` → `ms_uni41001`
2. 41001 LiteLLM 变为14组：7组glm5.1(k1~k7 × 10v) + 7组dsv4p(k1~k7 × 10v) = 140 dep
3. proxy 40001 glm5.1路由从41003→41001，dsv4p路由从42001→41001，且proxy实现variant+key 2D顺序轮询

**41003容器保留但不再路由。42001容器保留但不再路由。本地(opc2_uname)暂不更新。**

## 轮询设计

**正常request轮询（2D counter）：**
```
counter = _vk_rr_counter[mapped_model]  # 0..∞
variant_idx = (counter // NUM_KEYS) % NUM_VARIANTS
key_idx = counter % NUM_KEYS
counter += 1
→ litellm_model = f"{model_base}v{variant_idx+1}k{key_idx+1}"
  例: glm5.1v1k1, glm5.1v1k2, ..., glm5.1v1k7, glm5.1v2k1, ..., glm5.1v10k7
```

轮询序列（N=0到69循环）：
```
N=0:  v1+k1 → glm5.1v1k1
N=1:  v1+k2 → glm5.1v1k2
...
N=6:  v1+k7 → glm5.1v1k7
N=7:  v2+k1 → glm5.1v2k1
...
N=69: v10+k7 → glm5.1v10k7
N=70: 回到v1+k1
```

**429 cycling策略：**
- 429时：同variant换下一个key（key+1），与当前R19逻辑一致
- 7 key全429（同variant） → 返回429给agent（所有key的token quota耗尽，换variant不会改变quota）
- 不做variant-level cycling（所有key共享token quota，换variant无效）

## 1. LiteLLM 41001 config (`configs/litellm-glm51/config.yaml`)

从77059行(7000 dep) → ~840行(140 dep)

**model_name 格式：`{base}v{N}k{K}`**

glm5.1 10 variants:
- v1: ZHIPUAI/GLM-5.1, v2: ZHIPUAI/GLm-5.1, v3: ZHIPUAI/GlM-5.1, v4: ZHIPUAI/Glm-5.1
- v5: ZHIPUAI/gLM-5.1, v6: ZHIPUAI/gLm-5.1, v7: ZHIPUAI/glM-5.1, v8: ZHIPUAI/glm-5.1
- v9: ZHIPUAi/GLM-5.1, v10: ZHIPUAi/GLm-5.1

dsv4p 10 variants (从11→10，删除第11个 DeEpSeek-V4-Pro):
- v1: deepseek-ai/deepseek-v4-pro, v2: deepseek-ai/Deepseek-V4-Pro, v3: deepseek-ai/DeepSeek-v4-pro
- v4: deepseek-ai/DeepSeek-v4-Pro, v5: deepseek-ai/DeepSeek-V4-PrO, v6: deepseek-ai/DeepSeek-V4-PRo
- v7: deepseek-ai/DeepSeeK-V4-Pro, v8: deepseek-ai/DeepSeEk-V4-Pro, v9: deepseek-ai/DeepSEek-V4-Pro
- v10: deepseek-ai/DeePSeek-V4-Pro

总70 glm5.1 dep + 70 dsv4p dep = 140 dep

router_settings保持: num_retries=2, cooldown_time=10, simple-shuffle (proxy精确指定model，LiteLLM只做转发)

**dsv4p的allowed_openai_params不包含thinking相关参数**（dsv4p不支持thinking）

## 2. docker-compose.yml 变更

- `glm5.1_uni41001` service → `ms_uni41001`
  - container_name: ms_uni41001
  - nofile: 8192→2048 (140 dep vs 7000)
  - memory: 2048→1024M
  - start_period: 180→60s
  - CPU: 2.0→1.0
  - environment: 加 NUM_VARIANTS_GLM51=10, NUM_VARIANTS_DSV4P=10

- `auth_to_api_40001` / `auth_to_api_40002`:
  - LITELLM_URL_GLM51: → ms_uni41001:4000/v1/chat/completions
  - LITELLM_MODELS_URL_GLM51: → ms_uni41001:4000/v1/models
  - LITELLM_URL_DSV4P: → ms_uni41001:4000/v1/chat/completions
  - LITELLM_MODELS_URL_DSV4P: → ms_uni41001:4000/v1/models
  - NUM_VARIANTS_GLM51=10, NUM_VARIANTS_DSV4P=10 (新增)
  - depends_on: ms_uni41001 (替代 glm5.1_test41003 + dsv4p_uni42001)

- `glm5.1_test41003` service: 保留不变（容器定义保留，但不路由）
- `dsv4p_uni42001` service: 保留不变（容器定义保留，但不路由）

## 3. proxy.py 变更

### 核心逻辑变更

**新增配置：**
```python
NUM_VARIANTS_GLM51 = int(os.environ.get("NUM_VARIANTS_GLM51", "10"))
NUM_VARIANTS_DSV4P = int(os.environ.get("NUM_VARIANTS_DSV4P", "10"))
NUM_VARIANTS = {"glm5.1": NUM_VARIANTS_GLM51, "dsv4p": NUM_VARIANTS_DSV4P}

GLM51_VARIANT_IDS = [
    "ZHIPUAI/GLM-5.1", "ZHIPUAI/GLm-5.1", "ZHIPUAI/GlM-5.1", "ZHIPUAI/Glm-5.1",
    "ZHIPUAI/gLM-5.1", "ZHIPUAI/gLm-5.1", "ZHIPUAI/glM-5.1", "ZHIPUAI/glm-5.1",
    "ZHIPUAi/GLM-5.1", "ZHIPUAi/GLm-5.1",
]

DSV4P_VARIANT_IDS = [
    "deepseek-ai/deepseek-v4-pro", "deepseek-ai/Deepseek-V4-Pro", "deepseek-ai/DeepSeek-v4-pro",
    "deepseek-ai/DeepSeek-v4-Pro", "deepseek-ai/DeepSeek-V4-PrO", "deepseek-ai/DeepSeek-V4-PRo",
    "deepseek-ai/DeepSeeK-V4-Pro", "deepseek-ai/DeepSeEk-V4-Pro", "deepseek-ai/DeepSEek-V4-Pro",
    "deepseek-ai/DeePSeek-V4-Pro",
]

VARIANT_IDS = {"glm5.1": GLM51_VARIANT_IDS, "dsv4p": DSV4P_VARIANT_IDS}
```

**2D counter替换1D counter：**
```python
_vk_rr_counter = {}  # model → int counter (0..∞)
_vk_rr_lock = threading.Lock()

def _next_variant_key_pair(model: str) -> tuple:
    """Get next (variant_idx, key_idx) for 2D round-robin."""
    num_variants = NUM_VARIANTS.get(model, 10)
    with _vk_rr_lock:
        counter = _vk_rr_counter.get(model, 0)
        variant_idx = (counter // NUM_KEYS) % num_variants
        key_idx = counter % NUM_KEYS
        _vk_rr_counter[model] = counter + 1
        return (variant_idx, key_idx)
```

**Key cycling逻辑：**
```python
start_pair = _next_variant_key_pair(mapped_model)
start_variant_idx = start_pair[0]
start_key_idx = start_pair[1]
num_variants = NUM_VARIANTS.get(mapped_model, 10)

for attempt_idx in range(NUM_KEYS):
    current_key_idx = (start_key_idx + attempt_idx) % NUM_KEYS
    litellm_model = f"{litellm_model_base}v{start_variant_idx+1}k{current_key_idx+1}"
    # ... 429 → continue (try next key, same variant)
```

### _is_key_group_name 更新

过滤 `{base}v{N}k{K}` 格式从/v1/models响应：
```python
def _is_routing_name(name: str) -> bool:
    for base in MODEL_UPSTREAMS:
        for vi in range(NUM_VARIANTS.get(base, 10)):
            for ki in range(NUM_KEYS):
                if name == f"{base}v{vi+1}k{ki+1}":
                    return True
    return False
```

### /v1/models endpoint

从ms_uni41001的/v1/models获取模型列表，过滤掉所有v+k routing names。
只保留 glm5.1, dsv4p 等agent-facing名称。

### MODEL_UPSTREAMS 默认URL更新

```python
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": ... ms_uni41001:4000/v1/chat/completions ...
        "models_url": ... ms_uni41001:4000/v1/models ...
    },
    "dsv4p": {
        "chat_url": ... ms_uni41001:4000/v1/chat/completions ...
        "models_url": ... ms_uni41001:4000/v1/models ...
    },
}
```

### Env vars 新增

docker-compose.yml 中 proxy containers 添加：
- NUM_VARIANTS_GLM51=10
- NUM_VARIANTS_DSV4P=10

### 注释/docstring更新

proxy.py头部注释更新架构描述。

## 4. dsv4p config (`configs/litellm-dsv4p/config.yaml`)

**保持原样不动**。42001不再被proxy路由，config保留供参考或手动切回时使用。

## 5. dsv4p variants: 11→10

删除第11个variant `deepseek-ai/DeEpSeek-V4-Pro`。
影响：每个key失去200/id/day额度（7 key × 200 = 1400 req/day减少）。
用户明确要求10 per group。

## 6. DEPLOY_STATUS.md 更新

记录R21变更内容和部署步骤。

## 7. 风险评估

**高风险**：
- proxy路由全部指向41001 → 单点故障（41001挂 = glm5.1+dsv4p都不可用）
- variant-level轮询是proxy逻辑重大修改
- dsv4p从11→10 variants，减少额度

**缓解**：
- 41003/42001容器定义保留，可随时切回
- proxy env var可随时改回41003/42001
- 本地opc2_uname不更新

## 8. 不可变更约束检查

| 约束 | 检查结果 |
|------|---------|
| variant model IDs | glm5.1 10个保留(不���)；dsv4p 11→10(用户主动删除1个) |
| rpm=1 | 不变 |
| frontend model_name | glm5.1/dsv4p 不变 |
| ports | 41001, 40001, 40002 不变 |
| container name | 用户主动改名 glm5.1_uni41001 → ms_uni41001 |

## 文件修改清单

1. `configs/litellm-glm51/config.yaml` — 完全重写（77059行→840行，140 dep）
2. `configs/docker-compose.yml` — 容器名改、env var改、depends_on改、资源改
3. `configs/proxy/proxy.py` — 2D轮询逻辑、MODEL_UPSTREAMS默认URL、_is_routing_name、env vars
4. `configs/DEPLOY_STATUS.md` — 记录R21变更
5. `CLAUDE.md` — 更新架构描述、容器名、variant数、轮询描述