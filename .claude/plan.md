# R18 Optimization Plan — CC Switch-Inspired Architecture Enhancement

## 前提确认

**⚠️ 所有新功能必须在远程(opc_uname)先实验验证稳定后，才考虑部署到本地(opc2_uname)**

远程主机(opc_uname)是试验场。本地(opc2_uname)必须保持稳定。

## 一、配置一致性核查结果（已完成）

| 检查项 | 结果 | 状态 |
|--------|------|------|
| proxy.py disk==repo | MD5一致，1718行 | ✅ |
| docker-compose.yml disk==repo | MD5一致 | ✅ |
| litellm-glm51-test(41003) disk==repo | diff空 | ✅ |
| litellm-dsv4p(42001) disk==repo | diff空 | ✅ |
| litellm-glm51(41001) disk→repo **已修复** | 旧repo=256dep, 新=7000dep, header修正为"BACKUP" | ✅ 刚修复 |
| settings.json running==repo | 一致 | ✅ |
| shell env vars (non-interactive+login) | 全部存在 | ✅ |
| CHARS_PER_TOKEN_ESTIMATE | 运行=3.0，docker-compose=3.0，无discrepancy | ✅ |
| 所有容器 | 6个全healthy | ✅ |
| glm5.1/dsv4p 功能测试 | 200 OK | ✅ |
| context_window | 170000 | ✅ |
| 06-11 metrics | 100% success (260 reqs) | ✅ |
| 06-10 metrics | 99.8% success (1887 reqs) | ✅ |
| InternalServerError/500 历史数据 | 0 occurrences | ✅ |

**修复的问题**: 41001备份配置repo版本是旧的256-dep版本，disk是7000-dep版本。
**已修复**: 同步disk→repo，并修改header注释标记为"[BACKUP]"。

## 二、CC Switch类项目调研 — 可借鉴的ideas

调研了以下项目并提取技术要点：
- **Nyro** (nyroway/nyro): 跨协议网关，支持Anthropic/OpenAI/Gemini三种协议互转
- **claude-proxy** (yinxulai): Anthropic→OpenAI格式转换代理，动态URL路由
- **Portkey Gateway**: null_response_handling, retry配置化
- **其他趋势项目**: ccproxy, claude-relay-service, Wei-Shaw/claude-relay-service(12k stars)

### 值得借鉴的ideas（参考但不照搬）

| Idea | 来源 | 我们的适配方案 |
|------|------|--------------|
| **Circuit breaker** | Nyro | 对LiteLLM上游加circuit breaker：连续3次500/null→标记unhealthy→30s冷却→半开试探。**当前数据无500错误，这是预防性功能** |
| **null_response_handling** | Portkey | LiteLLM返回500+null choices时自动retry 1次（不是deployment rotation，是请求级retry让LiteLLM换dep） |
| **41001→41003 自动Failover** | Nyro per-target | 41003连续失败时→自动路由到41001(BACKUP)→30s后试探41003→成功回PRIMARY |
| **健康探针分级** | Nyro | `/health`→liveness(仅200), `/readyz`→readiness(检查41003+42001可达性) |
| **协议感知路由** | Nyro | `/v1/chat/completions` passthrough增强（供OpenCode等Agent），circuit breaker同样适用 |
| **配置动态化** | 多个 | MODEL_MAP从配置文件加载而非硬编码，新Agent可快速添加映射 |

### 不适合借鉴的ideas（与我们的场景不兼容）

| Idea | 原因 |
|------|------|
| 语义缓存(embedding-based) | 太复杂加延迟，单用户场景不需要 |
| Cloudflare Workers | 本地Docker部署 |
| SQLite/PostgreSQL config存储 | env vars + config.yaml更简单 |
| 多租户per-key quotas | 单用户 |
| async aiohttp重写proxy.py | http.server已稳定运行100%成功率，重写风险大于收益 |

**关键决策**: 保持http.server（不重写为asyncio）。原因：
1. 当前proxy.py已经100%成功率运行，34.88MiB/512MiB资源占用极低
2. ThreadingTCPServer对当前请求量(~260 reqs/day)完全够用
3. asyncio重写需要完全重建proxy镜像，风险远大于收益
4. circuit breaker/null retry可以安全地加在现有框架上

## 三、优化方案（5个Phase，渐进式）

### P1: Circuit Breaker + null_response_handling（最关键，先远程实验）

**目标**: 增加LiteLLM上游的circuit breaker和null choices处理

**设计**:
```
请求 → proxy → LiteLLM(41003/42001)
              ↓ 如果LiteLLM返回500+null choices
              → null_retry 1次（LiteLLM自动换dep，大概率成功）
              
              ↓ 如果同一upstream连续3次500/null
              → circuit breaker触发
              → 30s内直接返回503给Agent（跳过失败的上游）
              → 30s后半开：试探1个请求
              → 成功→关闭breaker；失败→继续30s冷却
```

**实现方式**: 在proxy.py中增加，不新建容器

**circuit breaker关键设计**:
- per-upstream的breaker（41003一个，42001一个），不是全局
- breaker只处理LiteLLM返回的500/null，不处理429/502（429=rate_limit由CC backoff处理，502=connection由CC retry处理）
- breaker状态: CLOSED(正常) → OPEN(30s冷却) → HALF-OPEN(试探1次) → CLOSED/OPEN
- breaker触发条件: 连续3次upstream返回status>=500
- 现有resilience retry(401/403)和thinking_budget fix不受breaker影响（这些是400级别的修复性retry）

**null_retry关键设计**:
- 只在LiteLLM返回500 + error_body包含"InternalServerError"/"null"/"choices"时retry
- retry 1次，不增加总延迟上限（worst case +1次上游请求时间~15-25s）
- retry不是deployment rotation（LiteLLM自己做），是让LiteLLM重新选dep的机会

### P2: 41001→41003 自动Failover（P1稳定后实施）

**目标**: 41003(PRIMARY)的circuit breaker触发时，自动路由到41001(BACKUP)

**设计**:
```
proxy → 41003(PRIMARY) [breaker CLOSED]
        ↓ breaker OPEN (连续3次500)
        → 路由到41001(BACKUP)
        → 41003 breaker 30s后 HALF-OPEN
        → 试探41003：成功→回PRIMARY；失败→继续BACKUP
```

**实现方式**: 扩展MODEL_UPSTREAMS，增加failover_target字段

**docker-compose.yml改动**: auth_to_api_40001 增加 `LITELLM_FAILOVER_URL_GLM51` env var指向41001

**关键约束**:
- 41001和41003配置完全一致（7000 dep + same router_settings），确保failover无缝
- failover只影响glm5.1路由，dsv4p没有failover（只有1个42001）
- 回PRIMARY优先（41003运行时间更长、deployment pool更健康）

### P3: 增强健康探针（P2稳定后实施）

**目标**: `/health`→仅liveness, `/readyz`→检查LiteLLM可达性

**设计**:
- `/health` → 不变（liveness only，避免fd exhaustion）
- `/readyz` → 新端点
  - 检查41003和42001的 `/health/liveliness`
  - 如果任一不可达 → 503 + `{"ready": false, "details": {...}}`
  - 如果全部可达 → 200 + `{"ready": true, "upstreams": {...}}`
  - timeout 5s per check（避免阻塞）
- 可用于Docker healthcheck替换当前的 `/health`

### P4: OpenAI passthrough增强 + 其他Agent适配（P3稳定后实施）

**目标**: `/v1/chat/completions` passthrough增加circuit breaker，支持OpenCode等Agent

**增强**:
- passthrough也经过circuit breaker保护
- model name映射：gpt-4o→glm5.1, dsv4p→dsv4p（可配置）
- stream_options.include_usage 自动注入
- 保留tool format不转换（OpenAI格式直接传给LiteLLM）
- `/v1/models` 对OpenAI格式请求也返回正确模型列表

**Codex `/v1/responses`**: 后期研究，需要新adapter（Responses API格式与Chat Completions完全不同）

### P5: 配置动态化（最远期，可选）

**目标**: MODEL_MAP从JSON配置文件加载

**设计**:
- `/app/model_map.json` 配置文件
- Docker volume mount，修改时无需重建镜像
- 支持新Agent的model name映射快速添加
- 启动时自动加载，修改后restart容器生效

## 四、实施顺序和风险控制

| Phase | 改动范围 | 风险 | 远程验证方式 |
|-------|---------|------|-------------|
| **P1** | proxy.py新增circuit breaker + null retry | **低**（预防性功能，当前0个500错误；不改变正常请求流程） | 重建proxy容器，功能测试glm5.1/dsv4p，观察24h metrics无变化 |
| **P2** | proxy.py扩展failover路由 | **中**（需要41001配置一致性已确认；新增路由逻辑） | 功能测试41003正常+41001可达，观察breaker触发场景 |
| **P3** | proxy.py新增 `/readyz` | **低**（新增端点，不影响现有功能） | curl /readyz验证，加入Docker healthcheck |
| **P4** | passthrough增强 | **低**（现有代码扩展） | OpenCode curl测试 |
| **P5** | 外部化配置 | **中**（需要修改Dockerfile） | 测试动态配置加载 |

**⚠️ 每个Phase完成后，必须在远程运行稳定24h+后才推进下一Phase**
**⚠️ 本地(opc2_uname)只在远程稳定后才同步，绝不提前部署**

## 五、本次commit内容

1. **修复41001备份配置同步**: repo litellm-glm51/config.yaml 从旧的256-dep版本更新为7000-dep版本（与disk一致），header注释标记为"[BACKUP]"
2. **R18计划文档**: 5个Phase的渐进式优化方案，CC Switch项目调研结果

**暂不实施任何P1-P5代码改动** — 等计划审批后，先在远程(opc_uname)实验P1。