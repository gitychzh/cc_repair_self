# auto-loop — Claude Code 自循环 Skill

**让 Claude Code 持续、无人值守地自动执行优化任务。**

## 这是什么？

auto-loop 是从 `cc_repair_self` 项目中提炼出来的通用 skill 框架。该项目让两台机器上的 Claude Code 通过共享 Git 仓库互相修复优化对方的配置，**完全无人值守运行了数月**，经过 18+ 轮迭代优化，成功率从 80% → 99.8%。

这个 skill 把那个实战验证过的自循环模式，提炼成一套**可复用、可部署**的通用框架。

## 核心模式

```
┌──────────────────────────────────────────────────┐
│                 CC 内循环                          │
│  CronCreate (每N分钟) → 注入 prompt → CC 执行     │ ← CC活着才有用
│  (分析 → 优化 → 验证 → push → 等下一轮)           │
└──────────────────────────────────────────────────┘
                     │
                     │ CC 卡死/崩溃？
                     ▼
┌──────────────────────────────────────────────────┐
│               Watchdog 外循环                      │ ← 不依赖CC进程
│  cron (每15分钟) → detect_stall → health_snapshot │
│  → 按决策矩阵:                                    │
│    正常 → 退出                                     │
│    卡死 → 注入/重启 CC                             │
│    不健康 → fix_infra → 再唤醒                     │
└──────────────────────────────────────────────────┘
```

**内循环**（CronCreate）驱动 CC 持续执行优化任务。**外循环**（watchdog）保障 CC 进程存活、基础设施健康。两者互补：内循环依赖 CC 进程，外循环独立于 CC 进程。

## 文件结构

```
auto-loop/
  README.md                          ← 你正在读的
  ARCHITECTURE.md                    ← 架构详解（两层机制、数据流、关键设计决策）
  QUICKSTART.md                      ← 5分钟部署指南
  config.env.template                ← 所有可配置变量的集中模板
  deploy.sh                          ← 一键部署脚本

  templates/
    watchdog/                        ← 外循环（watchdog）
      cc_watchdog.sh                 ← 主入口（cron 调用）
      detect_stall.sh                ← 卡死检测（jsonl mtime ≥ 10min）
      wake_claude.sh                 ← 双保险唤醒（inject → restart）
      health_snapshot.sh             ← 基础设施健康快照
      fix_infra.sh                   ← 自动修复（restart → pull → rollback）
      install.sh                     ← 安装/卸载脚本
      lib/log.sh                     ← 统一日志库

    cc-session/                      ← CC session 管理
      restart_claude.sh              ← 杀+重启 CC（screen + --resume）
      start.sh                       ← 一键启动自循环 session
      settings.json.template         ← CC 配置模板
      statusline-command.sh          ← 状态栏脚本（模型+上下文%）

    cron/                            ← CC 内循环配置
      scheduled_tasks.json.template  ← CronCreate 持久化文件模板
      register_cron.md               ← 如何注册 CronCreate 的详细指南
```

## 关键设计决策（实战验证）

1. **CC 必须在 screen 中运行** — watchdog 可以通过 `screen -X stuff` 注入文字唤醒 CC
2. **卡死检测基于 jsonl mtime，不是进程状态** — CC 进程可能在但卡死，mtime 是更可靠的信号
3. **唤醒双保险: 先注入 → 等 90s → 无效则重启** — 注入比重启轻量得多
4. **凌晨静默窗口** — 某些时段（如配额重置）只观察不操作
5. **防抖动冷却** — 5 分钟内不重复唤醒，避免反复重启的恶性循环
6. **--resume 恢复上下文** — 重启时恢复上次对话，避免丢失所有历史
7. **bash --login 启动 CC** — 确保 .profile → .bashrc 加载（env vars 可用）
8. **watchdog 与 CC 项目分开目录** — 避免 watchdog 修改被 CC 的 git 操作覆盖

## 限制

- 仅适用于 Linux 环境（依赖 `screen`、`stat -c %Y`、`pgrep` 等 Linux 工具）
- CC session 必须用 `--permission-mode bypassPermissions` 运行（无人值守需要）
- CronCreate 是 CC 内置功能，非 CC SDK 的标准 API，可能随版本变化
- watchdog 的 `screen -X stuff` 注入方式依赖于终端模拟，在某些环境下可能不稳定

## 快速开始

见 [QUICKSTART.md](QUICKSTART.md)。5 分钟部署。