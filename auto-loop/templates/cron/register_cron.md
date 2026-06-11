# 注册 CronCreate 定时任务指南

## 什么是 CronCreate？

CronCreate 是 Claude Code 内置的工具，可以注册 cron 定时任务。
每次 cron 触发时，会向当前 CC session 注入一条 prompt，驱动 CC 自动执行逻辑。

## 两种注册方式

### 方式 1: 在 CC 对话中调用（推荐）

进入 CC session:
```bash
screen -r claude
```

等待 CC 就绪，然后向 CC 说:
```
请帮我注册一个 CronCreate 定时任务：每 10 分钟执行一次，prompt 为 "检查远程仓库是否有更新..."
``

CC 会调用 CronCreate 工具，自动将任务写入 `~/.claude/projects/.../scheduled_tasks.json`。

### 方式 2: 手动编辑 scheduled_tasks.json

直接编辑 `~/.claude/projects/-<your-project-path>/scheduled_tasks.json`：

```json
{
  "tasks": [
    {
      "id": "a1b2c3d4",
      "cron": "*/10 * * * *",
      "prompt": "你的 prompt 内容",
      "createdAt": 1700000000000,
      "lastFiredAt": null,
      "recurring": true
    }
  ]
}
```

CC 进程启动时会自动读取此文件恢复定时任务。

## Prompt 设计要点

CronCreate 的 prompt 就是 CC 每轮自动执行的指令。好的 prompt 需要:

1. **明确循环逻辑**: "检查远程更新 → 如果有 → 分析 → 优化 → push"
2. **有数据支撑才修改**: "所有参数修改必须有日志数据支撑"
3. **没有更新时的行为**: "如果没有更新，只做日志分析"
4. **不可变约束提醒**: 可以在 prompt 中提到哪些东西不能改

### 示例 prompt（通用）

```
检查远程仓库是否有更新：git pull origin main。
如果有新更新，分析变更内容，对比本机配置进行优化适配。
所有参数修改必须有日志数据支撑（分析日志目录下的 metrics/error_detail）。
优化后更新部署状态文档并 push 到远程仓库。
如果没有更新，只做日志数据分析，检查是否有需要优化的参数，有数据支撑才修改。
```

## 重要注意事项

- **CronCreate 依赖 CC 进程**: CC 进程死了 → cron 任务不会触发。这是为什么需要外部 watchdog。
- **`recurring: true`**: 任务会持续循环直到被删除。
- **`durable: true`**: 任务会持久化到 `scheduled_tasks.json`，CC 重启后自动恢复。如果用 `durable: false`，只在内存中，CC 重启后丢失。
- **prompt 长度限制**: 尽量控制在 500 字以内，过长会导致注入延迟。
- **防冲突**: cron 间隔 ≥ 5分钟，避免与前一轮执行重叠。

## 删除定时任务

在 CC 对话中说:
```
请删除 id 为 a1b2c3d4 的定时任务
``

或使用 CronDelete 工具。