# cc_repair_self — Claude Code 自优化系统

两台机器（opc_uname ↔ opc2_uname）通过此共享 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。

## 你的身份

你是 **opc2_uname** 机器上的 Claude Code。你的任务是拉取此仓库，分析 **opc_uname**（本地机器）上的 cc-infra 配置，发现可优化的点，修改配置并测试，然后 push 回仓库让 opc_uname 拉取继续优化。

## 架构

```
Claude Code → :40001/40002 proxy (格式转换 + metrics + variant×key 2D round-robin)
             → :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 + dsv4pv1k1~v10k7 = 140 dep) [UNIFIED]
             → ModelScope API
```

核心原则：**proxy.py 做格式转换 + metrics logging + variant×key 2D round-robin + error cycling（request N → variant_idx=(N//7)%10, key_idx=N%7 → model `{base}v{V}k{K}`，429/500/502时同variant换下一个key，7 key全失败才返回agent），不做压缩、不做截断。LiteLLM 纯转发（proxy精确指定variant+key组合，num_retries=0，所有allowed_fails=0，避免浪费ModelScope quota）。压缩完全由 CC 内置 auto-compact 控制。**

**Variant×Key 2D Round-Robin (R21) 机制：**
- Proxy 维护 2D轮换 counter：request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS → model `glm5.1v{V}k{K}`
- 轮询序列：v1k1→v1k2→...→v1k7→v2k1→v2k2→...→v10k7→回到v1k1
- 429/500/502 时：同variant换下一个key（k→k+1），不变variant（ModelScope每种错误都扣quota，cycling避免浪费）
- 7 key 全部失败（同variant） → 根据错误类型分类返回：全429→rate_limit_error；有500→api_error；有502→api_error；有timeout→502 api_error
- 每个key的 cycling 尝试都记录在 error_detail 日志中（含variant_idx、error_type）
- 成功时：记录 variant_idx、key_idx 和 litellm_model，以及之前的 cycling 信息
- 不cycling的错误：400 input overflow、400 inappropriate content、400 thinking_budget InvalidParameter、401/403 AuthenticationError

## 不可变更约束（NEVER CHANGE）

| 约束 | 原因 |
|------|------|
| **所有 variant model IDs** | 每个变体model ID有独立额度200/id/天。删减变体=删减额度，**绝对禁止增删改**（R21用户主动删除dsv4p v11） |
| **rpm=1 per deployment** | 每个deployment限速1 RPM。**绝对禁止修改** |
| frontend model_name (agent-facing) | `glm5.1`, `dsv4p` — CC/agent请求使用这两个名字 |
| LiteLLM model_name (internal, R21) | `glm5.1v1k1`~`glm5.1v10k7`, `dsv4pv1k1`~`dsv4pv10k7` — proxy精确指定variant+key |
| Docker container names | `ms_uni41001`, `cc_postgres`, `auth_to_api_40001/40002` |
| port assignments | 41001=unified(ms_uni41001) |

### 10 Variant Model IDs（R21, ms_uni41001）

**GLM-5.1 (10 variants):**
`ZHIPUAI/GLM-5.1`, `ZHIPUAI/GLm-5.1`, `ZHIPUAI/GlM-5.1`, `ZHIPUAI/Glm-5.1`, `ZHIPUAI/gLM-5.1`, `ZHIPUAI/gLm-5.1`, `ZHIPUAI/glM-5.1`, `ZHIPUAI/glm-5.1`, `ZHIPUAi/GLM-5.1`, `ZHIPUAi/GLm-5.1`

**DSv4P (10 variants, R21 reduced from 11):**
`deepseek-ai/deepseek-v4-pro`, `deepseek-ai/Deepseek-V4-Pro`, `deepseek-ai/DeepSeek-v4-pro`, `deepseek-ai/DeepSeek-v4-Pro`, `deepseek-ai/DeepSeek-V4-PrO`, `deepseek-ai/DeepSeek-V4-PRo`, `deepseek-ai/DeepSeeK-V4-Pro`, `deepseek-ai/DeepSeEk-V4-Pro`, `deepseek-ai/DeepSEek-V4-Pro`, `deepseek-ai/DeePSeek-V4-Pro`

**已删除**: `deepseek-ai/DeEpSeek-V4-Pro` (dsv4p v11) — R21用户主动决定，每key减少200/id/day额度

## 关键原则（长期知识）

- **删除资源前必须验证其独立价值** — 曾删除11个变体model ID以为是"不支持的混合大小写"，但每个变体有独立200/id/day额度。正确流程：观察→测试→验证→决定。
- **proxy-level retry增加37%延迟** — 有proxy_retry的请求avg=15963ms vs 正常11635ms。proxy只做格式转换，retry由LiteLLM负责。
- **proxy绝不做截断/压缩** — proxy-level auto-compact导致灾难性上下文丢失。压缩只由CC内置auto-compact控制。
- **429→529 转换会导致CC崩溃** — 429=rate_limit(backoff retry)，529=overloaded(CC auto-compact→灾难性上下文丢失)。绝不转换。
- **CC v2.1.170+ startup check用shell env vars** — CC启动时的connectivity check用shell环境变量（ANTHROPIC_BASE_URL等），不读settings.json。必须三层保障：.bashrc（在non-interactive return之前）+.profile+restart_claude.sh用bash --login。
- **CC auto-compact质量远低于手动/compact** — 自动compact用stripNonEssential=true（截断tool输出，tools=[]），手动/compact用stripNonEssential=false（完整上下文）。CC提示compact时，主动/compact加自定义指令可获得更好的摘要。
- **/health endpoint会触发fd耗尽** — LiteLLM的/health触发per-deployment checks→OSError Too many open files。用/health/liveliness监控LiteLLM。Proxy的/health是简单状态检查（只返回{"status":"ok"}），SAFE用于Docker healthcheck。
- **CC tokenizer overestimates tokens ~1.7x** — 对中文+代码+JSON混合内容，Anthropic tokenizer估算值比ModelScope实际值高约1.7倍。autoCompactWindow必须考虑此偏差。
- **ModelScope双 quota 系统** — RPM quota（200/id/day/variant，ms_requests_remaining追踪）和 Token quota（per-key hourly/daily token allocation，无header追踪）是独立的。Jun 11 429 burst：RPM quota有1705 remaining但7个key的token quota同时耗尽→20个429。同一组key跨所有deployment，fallback到41001无效（同key=同token quota）。burst是暂时性的（15分钟自动恢复），不可通过配置修复。ModelScope对每种错误（429/500/502）都扣quota，所以必须在proxy层做error cycling而非让LiteLLM重试同一deployment。
- **proxy超时日志详细记录** — socket.timeout现在单独捕获（不再笼统归为ConnectionError），记录elapsed_ms、proxy_timeout_setting_ms、timeout_exceeded_by_ms（超了PROXY_TIMEOUT多少）。key cycling时的timeout也单独记录到error_detail。stream和collect_stream的超时同样详细记录。全key失败时区分429（rate_limit_error）vs timeout/connection（502 api_error），避免529→CC auto-compact灾难。

## 可调整参数（有数据支撑才能改）

### Proxy / Docker-compose.yml env

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| CHARS_PER_TOKEN_ESTIMATE | 3.0 | 1.5-6 | proxy用CPT估算tokens。Jun 11 metrics: actual chars/token(json)=4.08 median, proxy overestimates 1.36x (chars_json/3.0 vs actual)。只影响INPUT-WARN阈值，不影响CC auto-compact。3.0的overestimation提供安全提前警告 |
| MODEL_INPUT_TOKEN_SAFETY_GLM51 | 170000 | 120000-190000 | /v1/models报告的context_window |
| MODEL_INPUT_TOKEN_SAFETY_DSV4P | 170000 | 120000-190000 | 同上 |
| MAX_TOOL_DESC | 2000 | 800-4000 | 工具描述截断上限chars |
| MAX_SCHEMA_DESC | 600 | 300-1200 | Schema描述截断上限chars |
| PROXY_TIMEOUT | 300 | 120-600 | proxy请求超时秒。3天数据：P99=85s，max=210s，从未触发timeout |

### CC settings (claude/settings-*.json)

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| contextWindow | 170000 | 120000-190000 | CC认知的上下文容量上限 |
| autoCompactWindow | 155000 | 90000-180000 | CC自动compact触发阈值（est tokens） |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW | 155000 | 90000-180000 | env var，与autoCompactWindow对齐 |
| API_TIMEOUT_MS | 600000 | 300000-1200000 | CC→proxy HTTP总超时。R21改为600000(10min)，CC SDK默认值。数据：proxy cycling(7×2s 429) + 成功key(210s) = 224s，在600s内安全 |

### LiteLLM router_settings (41001 ms_uni41001, 14 groups × 10 dep each = 140 dep)

| 参数 | 当前值 | 说明 |
|------|--------|------|
| num_retries | 0 | R21：proxy处理所有error cycling，LiteLLM纯转发。单dep per model_name无fallback，重试只会浪费quota |
| cooldown_time | 10 | RPM 1-min窗口，10s proportional（不影响R21，proxy精确指定model_name） |
| routing_strategy | simple-shuffle | proxy精确指定model，LiteLLM只转发 |
| RateLimitErrorAllowedFails | 0 | 429 cycling由proxy处理，LiteLLM不重试（避免扣额外quota） |
| TimeoutErrorAllowedFails | 0 | timeout cycling由proxy处理 |
| InternalServerErrorAllowedFails | 0 | 500/choice:null cycling由proxy处理（R21新增） |
| AuthenticationErrorAllowedFails | 0 | |
| BadRequestErrorAllowedFails | 0 | |

### Proxy / Docker-compose.yml env (R21新增)

| 参数 | 当前值 | 范围 | 说明 |
|------|--------|------|------|
| NUM_VARIANTS_GLM51 | 10 | 5-10 | glm5.1每个key group的variant数 |
| NUM_VARIANTS_DSV4P | 10 | 5-11 | dsv4p每个key group的variant数（R21从11减为10） |

## 项目文件结构

```
configs/
  docker-compose.yml       # Docker编排（4个容器：cc_postgres, ms_uni41001, auth_to_api_40001/40002）
  .env.template             # 环境变量模板
  litellm-glm51/config.yaml       # 41001 LiteLLM配置（10v×7k glm5.1 + 10v×7k dsv4p = 140 dep）
  postgres/init-db.sh             # PostgreSQL初始化脚本
  proxy/
    Dockerfile / proxy.py         # 格式转换代理（variant×key 2D round-robin+metrics）
  claude/
    settings-opc_uname.json        # → ~/.claude/settings.json (API_TIMEOUT_MS=600000 ✅ R22)
    settings-opc2_uname.json       # → ~/.claude/settings.json (API_TIMEOUT_MS=600000 ✅ R22, 但opc2_uname本机仍=300000需同步)
    statusline-command-opc_uname.sh / statusline-command.sh
  DEPLOY_STATUS.md                 # 当前部署状态
scripts/
  backup_config.sh / deploy.sh / health_check.sh / restart_claude.sh / rollback.sh / sync_config.sh / ts_keepalive.sh
```

## 关键文件路径（opc_uname 本机）

| 文件 | 路径 | 修改后需 |
|------|------|----------|
| LiteLLM 配置 | `/opt/cc-infra/litellm-glm51/config.yaml` | `docker restart ms_uni41001` |
| 转换代理 | `/opt/cc-infra/proxy/proxy.py` | rebuild + recreate proxy容器 |
| Docker Compose | `/opt/cc-infra/docker-compose.yml` | `docker compose up -d --force-recreate` |
| 环境变量 | `/opt/cc-infra/.env` | recreate相关容器 |
| Claude设置 | `~/.claude/settings.json` | 重启claude进程 |
| Shell env vars | `.bashrc` + `.profile` + `/etc/environment` | 新终端生效 |

## 重启命令

```bash
# LiteLLM 配置变更（ms_uni41001）
docker restart ms_uni41001

# proxy.py 变更（需要重建镜像）
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002

# 全量重建（包括容器名变更 glm5.1_uni41001 → ms_uni41001）
cd /opt/cc-infra && docker compose up -d --force-recreate

# Claude Code 重启
bash ~/cc_ps/cc_recover/restart_claude.sh
```

## 每轮优化协议

1. **拉取** — `git pull`
2. **分析** — 读metrics/error_detail日志
3. **计划** — 必须说明WHY，附日志证据
4. **备份** — `bash scripts/backup_config.sh`
5. **执行** — 修改配置，重启受影响容器
6. **测试** — curl验证glm5.1和dsv4p返回200
7. **验证** — 读新metrics，对比前后
8. **记录** — 更新DEPLOY_STATUS.md
9. **Push** — 推送到GitHub

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
```

## 网络代理（opc_uname端如需）

```bash
systemctl --user start mihomo-sg.service
# SSH已配置~/.ssh/config自动通过443端口+代理
git push  # 自动走代理
```
