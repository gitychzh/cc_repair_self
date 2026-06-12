# cc_repair_self — Claude Code 互优化系统

两台机器（opc_uname ↔ opc2_uname）通过此 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。每轮必须包含日志数据分析。

完整项目文档见 **[CLAUDE.md](CLAUDE.md)**。

## 架构

```
CC → :40001/40002 proxy(格式转换+metrics+variant×key 2D round-robin) → :41001 LiteLLM ms_uni41001 (glm5.1+dsv4p) → ModelScope
```

proxy.py只做格式转换、metrics logging和variant×key 2D round-robin，error cycling由proxy处理。LiteLLM纯转发。

## 4容器

| 端口 | 容器 | 作用 |
|------|------|------|
| 40001 | auth_to_api_40001 | 格式转换代理 (variant×key 2D round-robin) |
| 40002 | auth_to_api_40002 | 格式转换代理 (opc2_uname端) |
| 41001 | ms_uni41001 | 统一 LiteLLM网关 (glm5.1 + dsv4p = 140 dep) |
| 5432 | cc_postgres | PostgreSQL |

## 不可变更约束

- **10 variant model IDs 禁止增删改**（每个变体200/id/day独立额度）
- **rpm=1 禁止修改**
- 详细变体ID列表见 [CLAUDE.md](CLAUDE.md)

## 优化轮次历史

| Round | Operator | Date | Summary |
|-------|----------|------|---------|
| opc_uname_r1 | opc_uname | 2026-05-31 | 修复全栈启动、2个独立LiteLLM网关、精简proxy.py(2289→784行)、11变体恢复、router对齐本地稳定参数 |
| opc2_uname_r1 | opc2_uname | 2026-05-31 | 41002→42001端口修正(41002为输入错误); 移除proxy-level retry(数据:+37%延迟); 增加metrics/error_detail logging; 增加input token safety check; LiteLLM timeout 120→180/300; request_timeout 600→300; lowest_latency_buffer 0→0.1; MAX_TOOL_DESC 800→2000; proxy memory 512→256M |
| opc_uname_r2 | opc_uname | 2026-06-01 | 全链路排查401卡死CC根因(enable_pre_call_checks health check标记全deployment unhealthy→retry被禁→401直达CC); disable enable_pre_call_checks; cooldown_time 30→120; lowest_latency_buffer 0.1→0.3; rolling_window_size 10→30; timeout 120→180同步; request_timeout 600→300同步 |
| opc2_uname_r2 | opc2_uname | 2026-06-01 | 深入排查远程opc2_uname 401复发根因: /health endpoint触发on-demand health check→choices=null→大量deployment被标记unhealthy→retry失败→401直达CC; 三层防御修复: 1)glm51 config增加background_health_checks:false(之前缺失); 2)proxy.py增加401 AuthenticationError resilience retry(收到401后自动重试一次让LiteLLM选不同deployment); 3)proxy.py修复URL path bug(_ensure_url_path自动补/v1/chat/completions); 两台机器全部重建重启测试通过 |