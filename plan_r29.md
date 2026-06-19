# R29: 三网关架构 — 40001(CC) + 40002(Codex) + 40003(OpenAI agents)

## 变更目标

将当前单一40001网关架构拆分为三个独立网关，各负责不同agent类型的格式转换：

| 网关 | 端口 | 负责agent | 格式转换 | 后端LiteLLM |
|------|------|-----------|----------|-------------|
| auth_to_api_40001 | 40001 | Claude Code (_cc) | Anthropic↔OpenAI | ms_uni41001 (41001) |
| auth_to_api_40002 | 40002 | Codex CLI (_cx) | Responses↔Chat Completions | ms_uni41001 (41001) |
| auth_to_api_40003 | 40003 | OpenClaw(_ol)/OpenCode(_oc)/Hermes(_hm) | OpenAI直通(passthrough) | ms_uni41001 (41001) |

**关键简化**：40003网关只处理OpenAI格式agent（_ol/_oc/_hm），这些agent本身发送的请求就是OpenAI格式，不需要任何格式转换，只需v×k cycling + passthrough。这意味着：
- 40001: 完整的Anthropic→OpenAI转换 + streaming转换 + thinking处理
- 40002: Responses↔Chat Completions双向转换 + streaming转换（使用codex.py）
- 40003: 最简单的OpenAI passthrough（直接转发，只做v×k key routing + error cycling）

## 远程opc_uname当前状态

- 运行3个容器：`cc_postgres`, `ms_uni41001`, `auth_to_api_40001`
- 无 `ms_uni41002`（无fallback LiteLLM）
- 无 `auth_to_api_40002`（无fallback proxy）
- proxy代码没有R26 LiteLLM fallback功能（所有key失败→返回错误）
- proxy代码没有R27 UPSTREAM_TIMEOUT（仍用PROXY_TIMEOUT=300做HTTPConnection timeout）

## 实施步骤

### Step 1: 同步本地代码到远程proxy目录
将本地最新的proxy gateway代码（含R27/R28优化）同步到远程 `/opt/cc-infra/proxy/gateway/`，但**关键修改**：
- **config.py**: 添加UPSTREAM_TIMEOUT env var支持（R27），但**移除**所有LiteLLM fallback URL（R26 LITELLM_FALLBACK_URL等）——远程只有41001一个LiteLLM，无fallback
- **upstream.py**: 移除LiteLLM fallback分支代码（R26 section），保留所有其他功能（v×k cycling、variant fallback、timeout cycling、thinking_budget fix等）
- **handlers.py**: 将`_make_upstream_conn`的timeout改为UPSTREAM_TIMEOUT（R27优化）
- 所有三个网关共用同一份proxy代码（只是LISTEN_PORT不同，代理代码本身是通用的）

### Step 2: 修改docker-compose.yml — 三网关+单LiteLLM架构

容器清单：
1. `cc_postgres` — PostgreSQL (不变)
2. `ms_uni41001` — LiteLLM (不变)
3. `auth_to_api_40001` — CC proxy (重建，env增加UPSTREAM_TIMEOUT=60)
4. `auth_to_api_40002` — Codex proxy (新建，LISTEN_PORT=40002)
5. `auth_to_api_40003` — OpenAI agents proxy (新建，LISTEN_PORT=40003)

所有proxy容器共用同一份proxy代码（build context: ./proxy），只是LISTEN_PORT不同。

**NOTES**：
- 不需要 `ms_uni41002` (fallback LiteLLM) — 用户只要求后端41001
- 不需要fallback proxy — 每个agent类型有自己的独立网关
- PostgreSQL只需要 `litellm_glm51` 一个DB（只有1个LiteLLM容器）
- `POSTGRES_MULTIPLE_DATABASES` 改为只创建 `litellm_glm51`

### Step 3: agent配置更新

- CC → 40001 (不变，`ANTHROPIC_BASE_URL=http://127.0.0.1:40001`)
- Codex → 40002 (`base_url=http://127.0.0.1:40002/v1`, `wire_api=responses`)
- OpenClaw/OpenCode/Hermes → 40003 (`baseUrl=http://127.0.0.1:40003/v1`)

### Step 4: 两步重建 — 阯止CC崩溃

不能同时重建所有proxy！CC正在通过40001运行。必须：
1. 先创建40002和40003（CC不使用它们，安全）
2. 重建40001时确保CC不会崩溃 — 先停40001 → 快速重建 → 启动

### Step 5: 测试验证

每个网关独立测试：
- 40001: CC Anthropic格式测试 (`curl /v1/messages`)
- 40002: Codex Responses格式测试 (`curl /v1/responses`)
- 40003: OpenAI格式测试 (`curl /v1/chat/completions` with _ol/_oc/_hm)
- 同时确认41001 LiteLLM健康

### Step 6: 更新CLAUDE.md和DEPLOY_STATUS.md

反映新架构：3网关+1 LiteLLM，端口分配变更，agent配置变更。

## proxy代码变更清单

### config.py 修改
1. 添加 `UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "60"))` (R27)
2. 移除所有 `fallback_chat_url` / `fallback_models_url` 字段 — MODEL_UPSTREAMS只有primary URL
3. 保留所有其他功能不变

### upstream.py 修改
1. 移除R26 LiteLLM fallback分支（all_conn_err → fallback LiteLLM cycling）
2. import中移除fallback URL相关变量
3. 保留所有其他功能不变（v×k cycling、variant fallback、timeout cycling等）

### handlers.py 修改
1. `_make_upstream_conn` 使用 `UPSTREAM_TIMEOUT` 替代 `PROXY_TIMEOUT`
2. import添加 `UPSTREAM_TIMEOUT`

### stream.py 修改（如果有fallback相关）
检查是否有需要移除的fallback代码

### codex.py 修改（如果有fallback相关）
检查是否有需要移除的fallback代码

### 不修改的文件
- converters.py — 无fallback相关代码
- error_mapping.py — 无fallback相关代码
- logger.py — 无fallback相关代码
- __init__.py — 无fallback相关代码
- gateway_main.py — 无fallback相关代码
