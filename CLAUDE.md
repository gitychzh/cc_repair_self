# cc_repair_self — Claude Code 自优化系统

两台机器（opc_uname ↔ opc2_uname）通过此共享 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。

## 你的身份

你是 **opc2_uname** 机器上的 Claude Code。你的任务是拉取此仓库，分析 **opc_uname**（本地机器）上的 cc-infra 配置，发现可优化的点，修改配置并测试，然后 push 回仓库让 opc_uname 拉取继续优化。

## 架构

```
Claude Code → :40001 proxy (格式转换 + metrics + input safety)
            → :41001 LiteLLM (glm5.1, 11变体×7keys=77 deployments)
            → :42001 LiteLLM (dsv4p, 11变体×7keys=77 deployments)
            → ModelScope API
```

核心原则：**proxy.py 只做格式转换和 metrics logging，LiteLLM 自带的功能（retry/fallback/routing/cooldown）不重复实现。proxy 不做 retry。**

## 不可变更约束（NEVER CHANGE）

| 约束 | 原因 |
|------|------|
| **11 variant model IDs** | 每个变体model ID有独立额度200/id/天，11×200=2200>账户总限额2000。删减变体=删减额度，**绝对禁止修改** |
| **rpm=1 per deployment** | 每个deployment限速1 RPM，7key×11variant=77 RPM/model。**绝对禁止修改** |
| frontend model_name | `glm5.1`, `dsv4p` — proxy/LiteLLM使用这两个名字 |
| Docker container names | `glm5.1_uni41001`, `dsv4p_uni42001`, `cc_postgres`, `auth_to_api_40001/40002` |
| port assignments | 41001=glm5.1, 42001=dsv4p |

### 11 Variant Model IDs（禁止增删改）

**GLM-5.1 (11 variants, 在 41001):**
`zhipuai/glm-5.1`, `ZHipuAI/GlM-5.1`, `ZhIpuAI/GLm-5.1`, `ZhiPuAI/gLM-5.1`, `ZhipUAI/GlM-5.1`, `ZhipuAi/GLM-5.1`, `ZhipuaI/GLm-5.1`, `zhipuAI/gLM-5.1`, `ZHIPUAI/GLM-5.1`, `zhipuai/GLM-5.1`, `ZhiPUAI/glm-5.1`

**DSv4P (11 variants, 在 42001):**
`deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`, `deepseek-ai/DeEpSeek-V4-Pro`

## 可调整参数（有数据支撑才能改）

| 参数 | 当前值 | 范围 | 所在文件 |
|------|--------|------|----------|
| num_retries | 5 | 2-12 | litellm config.yaml router_settings |
| cooldown_time | 30 | 10-300 | litellm config.yaml router_settings |
| RateLimitErrorAllowedFails | 3 | 0-10 | litellm config.yaml router_settings |
| TimeoutErrorAllowedFails | 2 | 0-10 | litellm config.yaml router_settings |
| AuthenticationErrorAllowedFails | 0 | 0-10 | litellm config.yaml router_settings |
| InternalServerErrorAllowedFails | 3 | 0-10 | litellm config.yaml router_settings |
| BadRequestErrorAllowedFails | 0 | 0-10 | litellm config.yaml router_settings |
| routing_strategy | simple-shuffle | simple-shuffle/latency-based-routing/random | litellm config.yaml |
| timeout (glm5.1) | 300 | - | litellm config.yaml |
| timeout (dsv4p) | 300 | - | litellm config.yaml |
| request_timeout | 300 | - | litellm config.yaml |
| MAX_TOOL_DESC | 2000 | 800-4000 | docker-compose.yml env |
| MAX_SCHEMA_DESC | 600 | 300-1200 | docker-compose.yml env |
| PROXY_TIMEOUT | 300 | 120-600 | docker-compose.yml env |
| MODEL_INPUT_TOKEN_SAFETY_GLM51 | 128000 | - | docker-compose.yml env |
| MODEL_INPUT_TOKEN_SAFETY_DSV4P | 128000 | - | docker-compose.yml env |
| CHARS_PER_TOKEN_ESTIMATE | 3.5 | 2-6 | docker-compose.yml env |

## 项目文件结构

```
configs/
  docker-compose.yml       # Docker编排（5个容器）
  .env.template             # 环境变量模板
  litellm-glm51/config.yaml # 41001 LiteLLM配置（11变体×7keys）
  litellm-dsv4p/config.yaml # 42001 LiteLLM配置（11变体×7keys）
  postgres/init-db.sh       # PostgreSQL初始化脚本
  proxy/
    Dockerfile              # proxy容器构建
    proxy.py                # 格式转换代理（仅格式转换）
  claude/
    settings-opc_uname.json  # opc_uname CC settings → ~/.claude/settings.json
    settings-opc2_uname.json # opc2_uname CC settings → ~/.claude/settings.json
    statusline-command-opc_uname.sh  # opc_uname statusline → ~/.claude/statusline-command.sh
    statusline-command.sh            # opc2_uname statusline → ~/.claude/statusline-command.sh
  DEPLOY_STATUS.md          # 当前部署状态
scripts/
  backup_config.sh          # 配置备份
  health_check.sh           # 健康检查
  restart_claude.sh         # Claude重启
  rollback.sh               # 配置回滚
logs/
  round_*_analysis.json     # 轮次分析数据
```

## 关键文件路径（opc_uname 本机）

| 文件 | 路径 | 修改后需 |
|------|------|----------|
| LiteLLM 配置 | `/opt/cc-infra/litellm-glm51/config.yaml` | `docker restart glm5.1_uni41001` |
| LiteLLM 配置 | `/opt/cc-infra/litellm-dsv4p/config.yaml` | `docker restart dsv4p_uni42001` |
| 转换代理 | `/opt/cc-infra/proxy/proxy.py` | rebuild + recreate proxy容器 |
| Docker Compose | `/opt/cc-infra/docker-compose.yml` | `docker compose up -d --force-recreate` |
| 环境变量 | `/opt/cc-infra/.env` | recreate相关容器 |
| Claude设置 | `~/.claude/settings.json` | 重启claude进程 |

## 重启命令

```bash
# LiteLLM 配置变更
docker restart glm5.1_uni41001
docker restart dsv4p_uni42001

# proxy.py 变更（需要重建镜像）
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001

# 全量重建
cd /opt/cc-infra && docker compose up -d --force-recreate

# Claude Code 重启
bash ~/cc_ps/cc_recover/restart_claude.sh
```

## 每轮优化协议

1. **拉取** — `git pull` 拉取对方push的变更
2. **分析** — 读metrics/error_detail日志，统计错误类型、频率、延迟
3. **计划** — 决定有数据支撑的变更（必须说明WHY，附日志证据）
4. **备份** — `bash scripts/backup_config.sh`
5. **执行** — 修改配置，重启受影响容器
6. **测试** — 发curl测试请求验证返回200，测试glm5.1和dsv4p
7. **验证** — 读新metrics，对比前后错误率
8. **记录** — 更新README轮次历史和logs/分析数据
9. **Push** — 推送到GitHub，让对方拉取继续优化

## 测试请求

```bash
# Anthropic格式（glm5.1）
curl -s -X POST http://127.0.0.1:40001/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-litellm-local" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# Anthropic格式（dsv4p）
curl -s -X POST http://127.0.0.1:40001/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-litellm-local" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"dsv4p","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# OpenAI格式
curl -s http://127.0.0.1:41001/v1/chat/completions \
  -H "Authorization: Bearer sk-litellm-local" \
  -d '{"model":"glm5.1","max_tokens":50,"messages":[{"role":"user","content":"test"}]}'
```

## 网络代理（opc_uname端如需）

```bash
systemctl --user start mihomo-sg.service
# SSH已配置~/.ssh/config自动通过443端口+代理
git push  # 自动走代理
```

## 反思教训

**删除资源前必须验证其独立价值。** 曾删除11个变体model ID以为是"不支持的混合大小写"，但每个变体有独立的200/id/day额度。删除=删除额度容量。正确流程：观察→测试→验证→决定。

**proxy-level retry增加37%延迟。** 数据证明：有proxy_retry的请求avg=15963ms vs 正常11635ms。proxy只做格式转换，retry由LiteLLM负责。