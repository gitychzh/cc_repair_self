# R31.5: 40005 网关代码深度工程化——物理拆分 cc 角色独立子目录

## 背景与决策（用户明确指示）
- 当前 gateway 是**三容器共享单镜像**架构：40001(cc)/40002(codex)/40003(passthrough)/40005(cc) 用同一份 `configs/proxy/gateway/` 代码，靠 `PROXY_ROLE` 环境变量差异化。
- **用户判断："合并是失败的，合并后各种 bug"** —— 共享代码导致任一容器改动波及全部（本轮前几轮反复踩坑：改 config.py 影响所有容器）。
- **决策：拆开。** 本轮只拆 40005（cc 角色），把 cc 需要的代码物理隔离到独立子目录 `configs/proxy/cc-proxy/`，删掉 cc 用不到的 codex/openai-passthrough 逻辑。40002/40003 暂不动（仍用现有共享镜像），下轮再拆。40005 与它们彻底解耦后，改 40005 不再影响 40002/40003。

## cc 角色(40005)实际依赖图（已核实）

`handlers._handle_messages`（/v1/messages，Anthropic 格式）只用到：
- **config.py** — 配置常量 + v×k counter 持久化（保留全部，是 cc 核心配置）
- **logger.py** — `_log/_log_metrics/_log_error_detail`（保留全部，41 行纯日志）
- **converters.py** — `anth_to_openai`/`openai_to_anth`/`_estimate_text_chars`（保留全部，cc 专用格式转换，无 codex 污染）
- **stream.py** — `stream_to_anth`/`collect_stream_to_anth`（保留全部，cc 专用 SSE，无 codex 污染）
- **upstream.py** — `execute_request`/`UpstreamResult`（保留，v×k cycling 核心，688 行）
- **error_mapping.py** — 只用 `convert_error`/`get_upstream_status_for_client`/`is_input_overflow`/`is_quota_exhaustion`（**裁剪**：删 `format_openai_error_*`×2 + `format_responses_error_*`×2，共 ~160 行）

**cc 用不到、要删除的：**
- `codex.py`（961 行，整个文件）— Responses API 处理
- handlers.py 里 `_handle_openai_with_cycling` + `_stream_openai_passthrough` + `_handle_codex_responses`（~250 行）
- handlers.py 里 `_proxy_models`（OpenAI 格式 /v1/models，cc 用 Anthropic 格式的）
- handlers.py `do_POST`/`do_GET` 里的 codex/passthrough/legacy 分支
- error_mapping.py 里 4 个 openai/responses 专用格式函数

## 实施方案：configs/proxy/cc-proxy/ 独立子目录

### 新目录结构
```
configs/proxy/cc-proxy/
  Dockerfile              # 独立镜像（FROM litellm base，COPY 本目录代码）
  gateway_main.py        # 入口（从现有精简）
  gateway/
    __init__.py
    app.py                # ThreadedHTTPServer + main
    config.py             # 从现有复制（保持 R31.3/R31.4 全部改动）
    logger.py             # 原样复制
    converters.py         # 原样复制
    stream.py             # 原样复制
    upstream.py           # 原样复制
    error_mapping.py      # 裁剪版（删 4 个 openai/responses 函数）
    handlers.py           # 精简版（只留 /v1/messages + Anthropic models + health）
```

### docker-compose.yml 改动
- `auth_to_api_40005` 的 `build.context` 从 `./proxy` 改为 `./proxy/cc-proxy`
- 其余 service（40001/40002/40003）的 build.context 保持 `./proxy` 不变 → **物理隔离成立**

### 行为不变性保证（零回归原则）
- cc-proxy 的 handlers.py 只保留 cc 路径，但**该路径的代码逐行保持与现版一致**
- 删除的是 `if PROXY_ROLE == "codex"` / `elif "passthrough"` / `else legacy` 这些**在 40005 里永远走不到的分支**
- 测试基准：`curl /v1/messages` 200、`/health` 返回 proxy_role=cc、`/v1/models` Anthropic 格式报 context_window=170000、角色隔离 404、counter 跨重启恢复

### 关键风险与缓解
| 风险 | 缓解 |
|------|------|
| 裁剪 error_mapping 误删 cc 要用的函数 | 已核实 cc 只用 4 个函数，另 4 个 format_* 是 openai/responses 专用 |
| upstream.py 里残留 openai/codex 引用 | 已核实 upstream.py 是 v×k cycling 核心，格式无关，原样复制 |
| 新 Dockerfile 构建失败 | 用与现版相同的 litellm base image，仅改 COPY 路径 |
| import 路径断裂 | 保持 gateway 包结构不变（`from .config import ...`），只是顶层目录不同 |

### 验证步骤
1. 构建 cc-proxy 镜像
2. `docker compose up -d --build --force-recreate auth_to_api_40005`
3. curl 测试 5 项：health / /v1/messages 200 / Anthropic models 170000 / OpenAI endpoint 404 / counter 持久化
4. **不影响 40001/40002/40003**（它们 build.context 没动）

## 本轮不做的（边界）
- 不拆 40002/40003（下轮）
- 不动 LiteLLM / docker-compose 其他 service
- 不改 cc 请求处理逻辑本身（只做物理隔离 + 删 dead code，不改运行时行为）
- 不做更大重��（函数级提取重复逻辑留作 cc-proxy 稳定后的后续轮）

## 工程化收益
- 40005 代码量从 ~4200 行 → ~2400 行（删 codex 961 + handlers 精简 ~250 + error_mapping ~160）
- 改 40005 物理上不可能影响 40002/40003
- cc 专属代码一目了然，长期维护清晰
- 为"稳定几天后再决定是否合并"提供干净的对照基线
