# R25: 独立40002副本 + Proxy层Upstream Fallback

## 背景
当40001背后的ms_uni41001重启（优化配置生效）时，所有连接到40001的agent会因为ConnectionRefusedError而死掉。当前40002 proxy也指向同一个ms_uni41001，所以40002也会同时不可用。

需要让40002成为完全独立的副本（有自己的LiteLLM后端），并且在proxy层增加upstream fallback——当primary LiteLLM容器不可达时，自动切换到fallback LiteLLM容器。Agent配置不需要修改。

## 新架构

```
Agent(CC/_cc)      → 40001(proxy) → primary: ms_uni41001(41001) | fallback: ms_uni42001(42001)
Agent(OpenClaw/_ol) → 40001(proxy) → primary: ms_uni41001(41001) | fallback: ms_uni42001(42001)
Agent(OpenCode/_oc) → 40001(proxy) → primary: ms_uni41001(41001) | fallback: ms_uni42001(42001)
Agent(Hermes/_hm)  → 40001(proxy) → primary: ms_uni41001(41001) | fallback: ms_uni42001(42001)
Agent(Codex)       → 40001(proxy) → primary: ms_uni41001(41001) | fallback: ms_uni42001(42001)

40002(proxy) → primary: ms_uni42001(42001) | fallback: ms_uni41001(41001)
```

- 40001 proxy: primary=ms_uni41001, fallback=ms_uni42001
- 40002 proxy: primary=ms_uni42001, fallback=ms_uni41001（对称配置）
- ms_uni42001: 完全复制ms_uni41001的配置（70 dep，相同的variants和keys）
- Agent配置不变（CC继续用40001，其他agent也继续用40001）

## Fallback触发条件

**只在ConnectionRefusedError时fallback**（LiteLLM容器完全不可达——正在重启）。

不在429/500/502时fallback，因为：
- 两个LiteLLM容器共享相同的7个ModelScope keys
- 同一个key在两个容器上的quota是共享的
- 429切换到另一个LiteLLM没用（同样的key=同样的quota耗尽）

**Fallback检测逻辑**：在key cycling中，如果**连续2次**ConnectionRefusedError（说明整个LiteLLM容器不可达，不是个别key问题），立即切换到fallback upstream。最多浪费2次连接尝试（约2秒），然后快速切换。

## 变更清单

### 1. 新增 configs/litellm-glm51-42001/config.yaml
- 完全复制 configs/litellm-glm51/config.yaml
- 70 dep glm5.1 (7 keys × 10 variants)
- 相同的 MS_KEY1~7, 相同的 variant model IDs
- 相同的 litellm_settings 和 router_settings

### 2. 修改 configs/docker-compose.yml
- 新增 `ms_uni42001` 服务（复制 ms_uni41001，端口42001:4000）
  - 相同的环境变量（MS_KEY1~7, MS_BASEURL, LITELLM_MASTER_KEY）
  - DATABASE_URL → litellm_glm51_42001（独立数据库）
  - volume: ./litellm-glm51-42001/config.yaml
  - volume: ./logs/litellm-glm51-42001:/app/logs
- 修改 `auth_to_api_40002` 指向 ms_uni42001
  - LITELLM_URL_GLM51: http://ms_uni42001:4000/v1/chat/completions
  - LITELLM_MODELS_URL_GLM51: http://ms_uni42001:4000/v1/models
- 新增 fallback 环境变量（给两个proxy）
  - 40001: LITELLM_FALLBACK_URL_GLM51=http://ms_uni42001:4000/v1/chat/completions, LITELLM_FALLBACK_MODELS_URL_GLM51=http://ms_uni42001:4000/v1/models
  - 40002: LITELLM_FALLBACK_URL_GLM51=http://ms_uni41001:4000/v1/chat/completions, LITELLM_FALLBACK_MODELS_URL_GLM51=http://ms_uni41001:4000/v1/models
- PostgreSQL POSTGRES_MULTIPLE_DATABASES 增加 litellm_glm51_42001

### 3. 修改 configs/proxy/gateway/config.py
- 新增 `FALLBACK_UPSTREAMS` dict（从环境变量读取）
- 新增 `LITELLM_FALLBACK_URL_GLM51` 和 `LITELLM_FALLBACK_MODELS_URL_GLM51` env vars
- 不改变现有的 MODEL_UPSTREAMS（primary不变）

### 4. 修改 configs/proxy/gateway/upstream.py
- execute_request() 新增 upstream fallback 逻辑：
  - 在key cycling loop中，检测连续ConnectionRefusedError模式
  - 如果连续2次ConnectionRefusedError → 切换到fallback upstream URL
  - 切换后继续正常的variant×key cycling（用相同的start variant+key）
  - variant fallback也使用fallback upstream URL
  - 新增日志标签: UPSTREAM-FALLBACK-SWITCH, UPSTREAM-FALLBACK-SUCCESS, UPSTREAM-FALLBACK-FAILED
  - 新增metrics字段: upstream_fallback=True, fallback_upstream_key="glm5.1"

### 5. 不修改的文件
- Agent配置（settings.json, .bashrc等）不变
- litellm-glm51/config.yaml 不变（ms_uni41001保持原样）
- Codex不需要新的agent suffix——fallback在proxy层透明处理

### 6. 更新 configs/DEPLOY_STATUS.md 和 CLAUDE.md
- 记录新架构（6个容器：cc_postgres, ms_uni41001, ms_uni42001, auth_to_api_40001, auth_to_api_40002）
- 记录upstream fallback机制
- 更新deploy命令和参数表

## Deploy顺序（关键！）

1. 创建 litellm-glm51-42001/config.yaml → 复制到 /opt/cc-infra/litellm-glm51-42001/
2. 更新 docker-compose.yml → 复制到 /opt/cc-infra/
3. 更新 postgres/init-db.sh → 增加 litellm_glm51_42001
4. 更新 proxy gateway代码 → 复制到 /opt/cc-infra/proxy/gateway/
5. 先启动 ms_uni42001: `docker compose up -d ms_uni42001`
6. 等待 ms_uni42001 healthy
7. 重建 proxy: `docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002`
8. 修改 auth_to_api_40002 环境变量后 recreate: `docker compose up -d --force-recreate auth_to_api_40002`
9. curl测试验证（40001和40002都返回200）
10. 测试fallback：docker restart ms_uni41001，验证40001 proxy自动切换到ms_uni42001

## 容器资源

ms_uni42001 与 ms_uni41001 相同：
- memory: 1024M (limits) / 512M (reservations)
- cpus: 1.0 (limits) / 0.25 (reservations)
- nofile: 2048 soft / 65536 hard
- 总新增内存约1GiB（LiteLLM容器），机器需要有足够内存

## 测试验证

```bash
# 1. 正常请求 — 40001
curl -s -X POST http://127.0.0.1:40001/v1/messages \
  -H "Content-Type: application/json" -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1_cc","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# 2. 正常请求 — 40002
curl -s -X POST http://127.0.0.1:40002/v1/messages \
  -H "Content-Type: application/json" -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1_cc","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# 3. Fallback测试 — 重启ms_uni41001，40001应自动fallback到ms_uni42001
docker restart ms_uni41001
# 立即发请求到40001（ms_uni41001还在重启中）
curl -s -X POST http://127.0.0.1:40001/v1/messages \
  -H "Content-Type: application/json" -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1_cc","messages":[{"role":"user","content":"test fallback"}],"max_tokens":50}'
# 检查proxy日志应看到 UPSTREAM-FALLBACK-SWITCH + UPSTREAM-FALLBACK-SUCCESS

# 4. OpenAI格式测试 — 40001
curl -s -X POST http://127.0.0.1:40001/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer sk-litellm-local" \
  -d '{"model":"glm5.1_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}'
```
