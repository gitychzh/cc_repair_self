# R38: Hermes 架构重新工程化 — 从 cc-infra 内部组件回归外部应用

## 问题

Hermes (`hermes-agent 0.17.0`) 是一个**独立的全局安装应用**，有自己的：
- 安装路径：`/home/opc_uname/.hermes-venv/`（pip 全局安装）
- 配置目录：`~/.hermes/config.yaml`（独立 YAML 配置体系）
- 状态目录：`~/.local/state/hermes/`
- 飞书 channel：`home_channel: feishu`
- 内置 fallback provider 机制：`fallback_providers: [litellm-local-ms]`

但 R37 把 Hermes 当成了 cc-infra 的"内部子系统"，造成了严重的架构错位：

| cc-infra 为 Hermes 做了什么 | 实际价值 |
|---|---|
| 1028行独立 hm-proxy（10个文件）| 功能与 passthrough-proxy 90% 重复 |
| 6个新 Docker 容器 | hm40006=146MiB(有用), 5 LiteLLM=~3GiB(**零日志**) |
| 端口 40006 + 41101-41105 | 5个 NV HM LiteLLM 完全无用途 |
| cc/codex/passthrough 里嵌入 `_hm` suffix | 仅 2次请求通过 40003（deploy smoke test） |
| sync_config.sh 16个 HM 文件映射 | 维护负担 |

## 核心洞察

**Hermes 不需要 cc-infra 替它管理 proxy。** Hermes 自己有完整的 provider/fallback 机制：

```yaml
# ~/.hermes/config.yaml (Hermes 自己的配置)
model:
  base_url: http://127.0.0.1:40006/v1    # primary = NV-only
  default: kimi_hm
  provider: litellm-nv-hm
providers:
  litellm-nv-hm: ...                       # NV primary
  litellm-local-ms: ...                     # MS fallback
fallback_providers: [litellm-local-ms]       # NV fail → fallback to MS
model_aliases:                              # 多个模型别名
  kimi: {base_url: 40006, model: kimi_hm}
  glm: {base_url: 40006, model: glm5.1_hm}
```

Hermes 的 fallback 和 multi-model 路由是**内置功能**，不需要 cc-infra proxy 来做。

## 重新工程化方案

### cc-infra 的职责边界

cc-infra 只负责：
1. **为 CC (Claude Code) 提供 proxy** — 40001/40005（核心使命）
2. **为 OpenAI-format agents 提供通用 passthrough proxy** — 40003（OpenClaw/OpenCode/任何 OpenAI client）
3. **为 Codex 提供 proxy** — 40002

Hermes 是 OpenAI-format client → 它只需要 cc-infra 提供 **一个可用的 API endpoint**。

### 方案：保留 hm40006 proxy，但精简到最小

hm40006 是**有用的** — 它为 Hermes 提供 NV-only routing（5 key sequential RR + per-key proxy + HTTPS CONNECT tunnel）。这个功能 passthrough-proxy(40003) 不提供（40003 是 MS-only）。

但 5 个 NV HM LiteLLM (41101-41105) 是**完全无用的** — 它们只是"monitoring"，日志为零，内存浪费 ~3GiB。

**具体操作：**

1. **删除 5 个 NV HM LiteLLM 容器** (ms_nv_hm_41101-41105)
   - 从 docker-compose.yml 移除 5 个服务定义
   - 删除 `configs/litellm-nv-hm/` 目录（5个 config YAML）
   - 从 postgres `POSTGRES_MULTIPLE_DATABASES` 移除 `litellm_nv_hm`
   - 停止并删除 5 个容器 → 释放 ~3GiB RAM
   - 从 sync_config.sh 移除 5 个 litellm-nv-hm 文件映射

2. **保留 hm40006 proxy** — 但承认它是"为外部 app 提供的 API endpoint"，不是 cc-infra 内部组件
   - hm-proxy 代码保留（1028行，独立 proxy，NV-only routing）
   - docker-compose.yml 里 hm40006 服务定义保留
   - 但标注清楚：这是"external app endpoint"，不是 cc-infra 核心组件
   - hm-proxy 不出现在 dispatcher 路由、蓝绿对比、auto_tune 循环中

3. **清理共享 proxy 的 `_hm` suffix 污染**
   - cc-proxy(40001/40005)：移除 `_hm` suffix 和 `glm5.1_hm` model mapping（CC 不用 _hm）
   - codex-proxy(40002)：移除 `_hm` suffix 和 `glm5.1_hm` model mapping（Codex 不用 _hm）
   - passthrough-proxy(40003)：**保留 `_hm` suffix**（作为 Hermes 的 MS fallback endpoint）
   - 这是因为 Hermes `fallback_providers: [litellm-local-ms]` 指向 40003，model name `glm5.1_hm` 需要 40003 能识别

4. **清理 CLAUDE.md 文档**
   - 从架构图中移除 Hermes 作为 cc-infra 内部组件的描述
   - 端口表：40006 标注为"external endpoint (Hermes NV proxy)"，41101-41105 标注为"removed"
   - 明确：cc-infra 只提供 endpoint，Hermes 自己管理 provider/fallback/routing

5. **保留 mihomo NV proxy 端口** (7894-7899)
   - hm40006 使用 per-key mihomo proxy（NV_PROXY_URL_MAP）
   - 这些端口为 NV API 提供美国代理，hm40006 和 cc-proxy(40005) 都用

6. **保留 Hermes agent config template** (`configs/agents/hermes-opc2_uname.yaml`)
   - 但更新注释：这是"Hermes 的参考配置"，不由 cc-infra 管理
   - Hermes 实际配置在 `~/.hermes/config.yaml`，由 Hermes 自己管理

### 不改的东西

| 不改 | 原因 |
|---|---|
| hm40006 proxy 代码 | 功能有用，NV-only routing Hermes 需要 |
| hm40006 docker 服务 | 为 Hermes 提供 NV endpoint |
| 40003 `_hm` suffix | Hermes fallback provider 指向 40003 |
| mihomo 7894-7899 | hm40006 和 40005 共用 NV proxy |
| Hermes ~/.hermes/config.yaml | Hermes 自己的配置，不属于 cc-infra |
| NV_MODEL_IDS (4 models) | kimi/glm/minimax/deepseek 在 hm-proxy config.py |
| NV keys (NV_KEY1-5) | hm40006 需要 5 key sequential RR |

### 资源释放

| 项目 | 当前 | 优化后 |
|---|---|---|
| Docker 容器数 | 12 (含 5 NV HM LiteLLM) | 7 (-5) |
| RAM 使用 | ~5GiB (含 3GiB 空 LiteLLM) | ~2GiB (-3GiB) |
| 端口占用 | 40006+41101-41105 | 40006 only (-5 ports) |
| 代码维护 | hm-proxy + 5 litellm config + _hm 污染 | hm-proxy only (-5 config, -3 proxy _hm 污染) |
| sync_config 映射 | 16 HM entries | ~10 (-5 litellm config + postgres) |

### 执行步骤

1. 停止并删除 ms_nv_hm_41101-41105 容器
2. 从 docker-compose.yml 删除 5 个 ms_nv_hm_4110X 服务定义
3. 从 postgres POSTGRES_MULTIPLE_DATABASES 移除 litellm_nv_hm
4. 删除 configs/litellm-nv-hm/ 目录
5. 从 sync_config.sh 移除 5 个 litellm-nv-hm 文件映射 + postgres DB entry
6. 从 cc-proxy/config.py 移除 `_hm` suffix + `glm5.1_hm` mapping
7. 从 codex-proxy/config.py 移除 `_hm` suffix + `glm5.1_hm` mapping
8. passthrough-proxy/config.py **保留** `_hm`（Hermes fallback 需要）
9. 从 cc-proxy/upstream.py 移除 unused _hm import
10. 从 codex-proxy/upstream.py 移除 unused _hm import
11. 更新 CLAUDE.md：Hermes 是外部 app，40006 是 external endpoint
12. 更新 DEPLOY_STATUS.md：移除 41101-41105
13. 更新 configs/agents/hermes-opc2_uname.yaml 注释
14. rebuild cc-proxy(40005) 和 codex-proxy(40002)
15. 验证：Hermes 仍能通过 40006 正常工作
