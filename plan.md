# R23: PROXY_TIMEOUT 2秒超时测试 + 三层超时冲突分析

## 背景

1. 远程 opc_uname 上运行的是 **R19 版本**的 gateway 代码（key-only round-robin，`glm5.1k1~k7`），不是仓库中的 R21 版本（v×k 2D round-robin，`glm5.1v{V}k{K}`）
2. R19 版本的 handlers.py **没有 socket.timeout 专门捕获**——所有错误（包括 timeout）走 `except Exception` → 标记为 `ConnectionError` → continue cycling
3. R19 版本所有 key 失败后统一返回 429 rate_limit_error，不区分 timeout vs 429 vs 500/502
4. 仓库中的 R21 版本已经有完整的 socket.timeout 捕获 + 分类错误返回逻辑

## 三层超时架构分析

```
CC (Claude Code)
  → API_TIMEOUT_MS = 600000 (10 min, CC→gateway HTTP总超时)
    → 40001 gateway
      → PROXY_TIMEOUT = 300s (gateway→LiteLLM HTTPConnection socket timeout)
        → LiteLLM ms_uni41001
          → request_timeout = 300s (litellm_settings)
          → per-deployment timeout = 300s
            → ModelScope API (实际TTFB 5-30s, 最长210s)
```

### 超时冲突点

| 层级 | 当前超时 | 测试超时 | 冲突风险 |
|------|----------|----------|----------|
| CC → gateway | 600s | 600s (不改) | ✅ 无冲突 |
| gateway → LiteLLM | 300s | **2s** | gateway 2s就断开，LiteLLM还在等ModelScope |
| LiteLLM → ModelScope | 300s | 300s (不改) | gateway已经timeout+cycling，LiteLLM继续等无所谓 |

**关键：gateway PROXY_TIMEOUT=2s 不与 CC/LiteLLM 超时冲突**。因为：
- CC 的 600s 远大于 gateway 的 2s×7keys=14s（全部超时场景）
- LiteLLM 自己的 300s 到 ModelScope 是独立的，gateway 2s timeout 后只是断开了与 LiteLLM 的连接
- LiteLLM 可能还在处理，但 gateway 已经 cycling 到下一个 key 了

## 测试方案

### 步骤1：先部署 R21 版本到远程

当前远程跑的是 R19，但仓库已经是 R21。R21 有完整的 socket.timeout 分类逻辑，这正是我们要验证的核心。先拉取+重建：

```bash
cd /opt/cc-infra
git pull
docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002
```

### 步骤2：修改 PROXY_TIMEOUT 为 2秒

修改 docker-compose.yml 两个 gateway 容器的 PROXY_TIMEOUT:
```yaml
PROXY_TIMEOUT: "2"  # 从 "300" 改为 "2"
```

同步到远程并重建容器。

### 步骤3：用户手动给 CC 布置任务

用户给 CC 一个正常任务，CC 发请求到 gateway → gateway 发到 LiteLLM → ModelScope 处理需要 >2s → gateway 2s 后 socket.timeout → cycling 到下一个 key → 又 2s timeout → ...

### 步骤4：观察日志约10分钟

我通过 SSH 监控 gateway 日志，观察：
1. 是否每个 key 都被逐一 timeout+cycling（R21 日志格式：`TIMEOUT v{V} k{K}/{NUM_KEYS}`)
2. cycling 后的错误类型（R21 应标记为 `SocketTimeout`）
3. 全 key 失败后返回给 CC 的错误（R21 应返回 502 api_error，因为有 timeout）
4. CC 收到 502 后的行为（retry → 又触发 7 keys timeout → 循环）
5. 最终 CC 是否会因为持续 502 而放弃或 600s 超时

### 步骤5：分析三层超时冲突

根据日志观察结果，分析：
- R21 的 socket.timeout 专门捕获是否正确触发（而非笼统 Exception）
- timeout 后 cycling 逻辑是否正确（同 variant 换下一个 key）
- 全 key timeout 后返回 502 api_error（而非 429 rate_limit_error）是否正确
- CC 的 600s 超时是否足够应对 gateway 的 cycling 延迟
- LiteLLM 连接被 gateway 2s 断开后是否正确处理（不影响下一个 key 的连接）

### 步骤6：恢复 PROXY_TIMEOUT 为 300秒

测试完成后恢复，避免影响正常使用。

## 发现的关键问题

1. **远程运行 R19，仓库是 R21**：Docker build 用仓库代码，但远程容器可能很久没重建了。必须先部署 R21。

2. **R19 timeout 标记不准确**：R19 把 socket.timeout 标记为 `ConnectionError`，不区分 timeout vs 真正的连接错误。全 key 失败后统一返回 429 rate_limit_error。在 PROXY_TIMEOUT=2s 测试中，所有 key 都会 timeout，但 R19 会把它们都当作 rate_limit 返回给 CC → CC backoff retry → 又 timeout → 循环。

3. **R21 timeout 分类正确**：仓库中的 R21 版本有 `socket.timeout` 专门捕获，区分 timeout vs 429 vs 500/502，全 key 失败后根据实际错误类型返回不同状态码。2s 超时测试中 R21 应返回 502 api_error（CC retry），而不是 429 rate_limit_error（CC backoff）。