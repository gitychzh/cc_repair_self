# cc_repair_self — Claude Code 自优化系统

两台机器（opc_uname ↔ opc2_uname）通过此 GitHub 仓库互相修复优化对方的 Claude Code 基础设施。

完整项目文档见 **[CLAUDE.md](CLAUDE.md)**，当前部署状态见 **[configs/DEPLOY_STATUS.md](configs/DEPLOY_STATUS.md)**。

## 当前架构 (R24.4)

```
                    :40001 proxy gateway (5 agent types)
                    ├── _cc (Claude Code)    → /v1/messages → Anthropic→OpenAI 转换
                    ├── _ol (OpenClaw)       → /v1/chat/completions → OpenAI 直通
                    ├── _oc (OpenCode)       → /v1/chat/completions → OpenAI 直通
                    ├── _hm (Hermes)         → /v1/chat/completions → OpenAI 直通
                    ├── _cx (Codex CLI)      → /v1/responses → Responses↔Chat Completions 转换
                    │
                    → :41001 LiteLLM ms_uni41001 (glm5.1v1k1~v10k7 = 70 dep)
                    → ModelScope API
```

## 3 容器

| 端口 | 容器 | 作用 |
|------|------|------|
| 40001 | auth_to_api_40001 | Proxy gateway (格式转换 + v×k 2D round-robin + error cycling + variant fallback) |
| 41001 | ms_uni41001 | 统一 LiteLLM 网关 (glm5.1 only = 70 dep) |
| 5432 | cc_postgres | PostgreSQL 16 |

## 不可变更约束

- **10 variant model IDs 禁止增删改**（每个变体有独立 200/id/day 额度）
- **rpm=1 per deployment 禁止修改**
- 详细变体ID列表见 [CLAUDE.md](CLAUDE.md)

## Agent 后缀系统

| 后缀 | Agent | 格式 |
|------|-------|------|
| `_cc` | Claude Code | Anthropic → /v1/messages |
| `_ol` | OpenClaw | OpenAI → /v1/chat/completions |
| `_oc` | OpenCode | OpenAI → /v1/chat/completions |
| `_hm` | Hermes | OpenAI → /v1/chat/completions |
| `_cx` | Codex CLI | Responses → /v1/responses |

无后缀 = `_cc`（向后兼容：`glm5.1` = `glm5.1_cc`）

## 优化轮次历史

| Round | Date | Summary |
|-------|------|---------|
| R1→R10 | 05-31→06-05 | 修复全栈启动、proxy精简、529→CC崩溃修复、/health fd耗尽、MODEL_MAP修复 |
| R11→R17 | 06-05→06-11 | proxy auto-compact移除、参数调优(compact/safety)、99.8%稳定性 |
| R18→R19 | 06-11→06-12 | tier路由、key round-robin(7组429 cycling)、timeout详细日志 |
| R20→R21 | 06-12 | 变体缩减、统一ms_uni41001容器(140 dep)、v×k 2D round-robin |
| R22→R23 | 06-12 | 429+500+502 error cycling、LiteLLM零retry、variant fallback+retry-after=180s、multi-agent gateway模块化 |
| R23.1→R24.4 | 06-12 | agent suffix系统、Codex Responses API支持、40002合并删除、dsv4p移除 |

详细变更见 git history 和 [DEPLOY_STATUS.md](configs/DEPLOY_STATUS.md)。

## 子项目

- **[auto-loop/](auto-loop/)** — 从本项目提炼出的 CC 自循环 skill 框架（内循环 CronCreate + 外循环 watchdog）
