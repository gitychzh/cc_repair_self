# 项目整理与归档计划

## 目标
提炼 sessions 教训、清理冗余文件、提交配置漂移修正（glm5.1→5.2 改名），让项目干净可接手。

## 1. 归档 Sessions（提炼后删除）

7 个历史 session → 提炼**独有**教训（不与现有 memory 重复）后删除 jsonl 原文：

| Session | 主题 | 提炼去向 |
|---------|------|----------|
| d5996a50 (972K) | glm5.1→5.2 全局改名（危险操作） | 新增 memory `glm51-to-52-rename.md`：全局改名必须先验证目标模型存在、注意 sed delimiter 与大小写变体、注释行残留 |
| 38ca193a (484K) | R29 三网关架构（40001/40002/40003） | 删除——R29 未实施，plan_r29.md 已决定删除；架构信息无独立价值 |
| 0c9a75f9 (616K) | 远程恢复单网关/去多 agent 配置 | 删除——操作型，教训已在 core-lessons |
| d4c18bee (1.3M) | 自优化轮次 | 删除——纯运行，教训已沉淀在 DEPLOY_STATUS/core-lessons |
| 2714d99c/995f9962/da7b38c1 | 自优化循环/接力 | 删除——cron-optimization-loop memory 已覆盖 |

**操作**：先创建 `glm51-to-52-rename.md` memory + 更新 MEMORY.md 索引，再 `rm` 7 个 jsonl（保留当前 session e80e2d35）。

## 2. 删除冗余文件

| 文件 | 原因 | 命令 |
|------|------|------|
| `plan.md` | R27 旧计划已落地，.gitignore 已含 `plan.md` | `rm plan.md`（未跟踪，直接删） |
| `plan_r29.md` | R29 三网关未实施，决定删除 | `rm plan_r29.md` + 加 `.gitignore` |
| `claude_output.log` | 启动乱码日志，.gitignore 已含 | `rm` |
| `configs/logs/proxy/proxy-40002/` | 旧 proxy 运行日志，不应进 git | `rm -rf configs/logs/` |
| `.claude/plans/project-cleanup-plan.md` | 上次清理计划，已执行完毕 | `git rm` |
| `.claude/scheduled_tasks.lock` | lock 文件，运行态产物 | `rm`（已 gitignore? 否则加） |

## 3. 更新 .gitignore

补充（防误提交）：
```
plan_r29.md
.claude/scheduled_tasks.lock
configs/logs/
```

## 4. 提交配置漂移（glm5.1→5.2 改名 + 文件清理）

工作区 20 文件的 diff 是 glm5.1→glm5.2 注释更新（真实配置修正）。按用户选择**只提交不 push**。

提交内容：
- glm5.1→5.2 改名 diff（docker-compose / config.yaml / proxy gateway / agents / settings / CLAUDE.md / README / DEPLOY_STATUS / scripts）
- 文件删除（plan.md/plan_r29.md/claude_output.log/configs/logs/.claude cleanup plan）
- .gitignore 更新

commit message: `chore: 整理归档 — glm5.1→5.2改名修正提交 + sessions/memory提炼 + 冗余文件清理`

**不 push**（按用户要求，留待用户确认）。

## 5. memory 去重核对

现有 10 个 memory 文件已较干净（core-lessons 已合并 14 条教训）。本轮只**新增 1 个**（glm51-to-52-rename），不删除现有。MEMORY.md 加一行索引。

## 执行顺序
1. 删除冗余文件（plan.md/plan_r29.md/claude_output.log/configs/logs/.claude cleanup plan/lock）
2. 更新 .gitignore
3. 创建 glm51-to-52-rename.md + 更新 MEMORY.md
4. 删除 7 个历史 session jsonl
5. `git add -A && git commit`（含 glm5.1→5.2 diff + 清理）—— **不 push**

## 不做
- 不改 CLAUDE.md / DEPLOY_STATUS.md 内容（已是最新 R28，无需重写）
- 不动 auto-loop/（独立子项目，已干净）
- 不动 backups/（已 gitignore，本地保留）
- 不 push（用户要求）
