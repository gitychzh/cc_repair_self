# cc_repair_self — Claude Code 互优化系统

两台机器（opc_uname ↔ opc2_uname）通过此 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。每轮必须包含日志数据分析。

完整项目文档见 **[CLAUDE.md](CLAUDE.md)**。

## 架构

```
CC → :40001 proxy(格式转换) → :41001 LiteLLM(glm5.1) → ModelScope
                              → :41002 LiteLLM(dsv4p)  → ModelScope
```

proxy.py只做格式转换，LiteLLM处理retry/fallback/routing。

## 5容器

| 端口 | 容器 | 作用 |
|------|------|------|
| 40001 | auth_to_api_40001 | 格式转换代理 |
| 40002 | auth_to_api_40002 | Codex格式转换代理 |
| 41001 | glm5.1_uni41001 | glm5.1 LiteLLM网关(77 deployments) |
| 41002 | dsv4p_uni41002 | dsv4p LiteLLM网关(77 deployments) |
| 5432 | cc_postgres | PostgreSQL |

## 不可变更约束

- **11 variant model IDs 禁止增删改**（每个变体200/id/day独立额度）
- **rpm=1 禁止修改**
- 详细变体ID列表见 [CLAUDE.md](CLAUDE.md)

## 优化轮次历史

| Round | Operator | Date | Summary |
|-------|----------|------|---------|
| opc_uname_r1 | opc_uname | 2026-05-31 | 修复全栈启动、2个独立LiteLLM网关、精简proxy.py(2289→784行)、11变体恢复、router对齐本地稳定参数(latency-based-routing/cooldown=30/retries=3) |
| opc2_uname_r1 | opc2_uname | — | 等待对方拉取后执行 |