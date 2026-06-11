# ARCHITECTURE.md — auto-loop 架构详解

## 两层循环机制

### 内循环：CronCreate 定时注入

**依赖**: CC 进程存活

**原理**: CC 内置 `CronCreate` 工具可以注册 cron 定时任务。每次触发时，会向当前 CC session 的对话中注入一条 prompt，CC 就像收到了用户消息一样处理它。

```
CronCreate 注册 (*/10 * * * *)
    ↓ 每10分钟
注入 prompt 到 CC 对话
    ↓ CC 像收到用户消息一样处理
CC 执行: git pull → 分析 → 优化 → push
    ↓ 完成后等待下一轮触发
下一轮 CronCreate 触发
    ↓
注入 prompt → CC 执行 ...
```

**关键特性**:
- `recurring: true` → 持续循环
- `durable: true` → 持久化到 `~/.claude/projects/.../.claude/scheduled_tasks.json`
- CC 进程启动时自动恢复已注册的任务
- CC 进程死亡 → 任务停止触发（但配置保留，重启后恢复）

**prompt 设计**:
prompt 就是 CC 每轮自动执行的指令。好的 prompt 需要:
1. 定义循环逻辑（"检查更新 → 如果有 → 分析 → 优化 → push"）
2. 定义无更新时的行为（"只做日志分析"）
3. 强调数据支撑（"所有修改必须有日志数据支撑"）
4. 可以提到不可变约束（"某些东西绝对不能改"）

### 外循环：Watchdog cron 脚本

**依赖**: 系统级 crontab（不依赖 CC 进程）

**原理**: 每 15 分钟执行一个 shell 脚本，检测 CC 是否卡死、基础设施是否健康，按决策矩阵采取行动。

```
crontab 触发 (*/15 * * * *)
    ↓
cc_watchdog.sh
    ↓
1. detect_stall.sh
   - 检查 CC session jsonl 文件的 mtime
   - 最新 jsonl ≥ STALL_THRESHOLD_SEC 秒无更新 → 卡死
    ↓
2. health_snapshot.sh
   - 检查 CC 进程/screen/端点/容器是否健康
    ↓
3. 决策矩阵
   ┌──────────┬──────────┬─────────────────────────┐
   │ 卡死？    │ 基础OK？ │ 动作                     │
   ├──────────┼──────────┼─────────────────────────┤
   │ 否       │ OK       │ 正常退出                  │
   │ 否       │ 不OK     │ fix_infra                 │
   │ 是       │ OK       │ 唤醒 CC                   │
   │ 是       │ 不OK     │ fix_infra → 唤醒          │
   └──────────┴──────────┴─────────────────────────┘
```

## 唤醒流程详解

```
卡死判定 (mtime ≥ 600s 无新写入)
    ↓
防抖动检查 (5分钟内不重复唤醒)
    ↓
┌── 阶段1: screen 注入 ──────────────────┐
│  screen -S claude -X stuff '\n继续\n'   │ ← 向终端注入文字
│  → 等待 90s                              │
│  → 每10s 检查 jsonl mtime 是否变化        │
│  → 有变化 → CC 已恢复 → exit 0           │
│  → 无变化 → 注入失败 → 进入阶段2         │
└─────────────────────────────────────────┘
    ↓ (注入失败)
┌── 阶段2: 重启 ──────────────────────────┐
│  1. 写 wakeup plan 到 ~/.claude/plans/   │ ← CC醒来立刻知道要做什么
│  2. kill CC 进程 (SIGTERM → 10s → SIGKILL) │
│  3. kill screen session                  │
│  4. screen -dmS claude bash --login -c   │
│     'claude --resume <session_id>'       │ ← 恢复上次对话
│  5. 验证 CC 进程在不在                    │
│  → 在 → OK                              │
│  → 不在 → 失败                           │
└─────────────────────────────────────────┘
```

**为什么双保险？**
- `screen -X stuff` 注入是最轻量的方式，CC 不需要重启，上下文完整保留
- 但注入可能因为 CC 卡死太深而无效（进程在但不响应）
- 重启是重武器，但 `--resume` 可以恢复大部分上下文
- 写 wakeup plan 确保 CC 醒来后不迷茫

## 基础设施修复流程

```
fix_infra.sh 触发
    ↓
1. hard_lint (可选)
   - 检查不可变约束是否被破坏
   - 被破坏 → exit 2 拒绝执行
    ↓
2. 三级修复策略（按序尝试，首次成功即停止）
    ↓
┌── 策略1: docker compose restart ─────┐
│  最轻: 只重启容器，不动配置              │
│  → 验证 3次健康检查                     │
│  → OK → exit 0                        │
└───────────────────────────────────────┘
    ↓ (失败)
┌── 策略2: git pull + 同步配置 ─────────┐
│  中等: 拉最新配置从Git仓库，复制到deploy  │
│  → docker compose up -d               │
│  → 验证 3次健康检查                     │
│  → OK → exit 0                        │
└───────────────────────────────────────┘
    ↓ (失败)
┌── 策略3: 回滚到最近 .bak ─────────────┐
│  重武器: 回滚所有配置到最近备份           │
│  → docker compose up -d               │
│  → 验证 3次健康检查                     │
│  → OK → exit 0                        │
│  → 不OK → exit 1 (人工介入)            │
└───────────────────────────────────────┘
```

## 数据流图

```
┌─────────────────────────────────────────────────────────┐
│                    Linux Host                            │
│                                                          │
│  ┌─────────────┐    ┌───────────────┐    ┌──────────┐  │
│  │  crontab    │───→│ cc_watchdog.sh│───→│ fix_infra│  │
│  │  */15       │    │               │    │          │  │
│  └─────────────┘    │ detect_stall  │    │ restart  │  │
│                     │ health_snap   │    │ pull+sync│  │
│  ┌─────────────┐    │ wake_claude   │    │ rollback │  │
│  │  CronCreate │───→│               │───→│          │  │
│  │  */10       │    └───────────────┘    └──────────┘  │
│  │  (CC 内置)  │                                    │  │
│  └─────────────┘    ┌───────────────┐                 │  │
│       │             │               │                 │  │
│       │ 注入prompt  │  screen       │                 │  │
│       └────────────→│  session      │                 │  │
│                     │  "claude"     │                 │  │
│                     │               │                 │  │
│                     │  ┌──────────┐ │                 │  │
│                     │  │ CC进程    │ │                 │  │
│                     │  │ (node)    │ │                 │  │
│                     │  └──────────┘ │                 │  │
│                     └───────────────┘                 │  │
│                                                          │
│  ┌───────────────────────────────────────┐              │  │
│  │ ~/.claude/                            │              │  │
│  │   settings.json (env, permissions)    │              │  │
│  │   projects/.../session/*.jsonl        │ ← detect 读此│  │
│  │   projects/.../.claude/scheduled_tasks│ ← CronCreate │  │
│  │   plans/...-auto-wakeup.md            │ ← wake 写此 │  │
│  └───────────────────────────────────────┘              │  │
│                                                          │
│  ┌───────────────────────────────────────┐              │  │
│  │ watchdog_dir/                         │              │  │
│  │   logs/watchdog.log                   │ ← 日志      │  │
│  │   state/last_wakeup.json              │ ← 防抖动    │  │
│  │   scripts/*.sh                        │ ← watchdog  │  │
│  └───────────────────────────────────────┘              │  │
│                                                          │
│  ┌───────────────────────────────────────┐              │  │
│  │ /opt/infra-dir/                       │              │  │
│  │   docker-compose.yml                  │ ← deploy    │  │
│  │   *.yaml configs                      │              │  │
│  │   *.bak.<timestamp> 备份              │ ← fix 写此  │  │
│  └───────────────────────────────────────┘              │  │
└─────────────────────────────────────────────────────────┘
```

## 关键设计决策详解

### 为什么用 jsonl mtime 检测卡死？

CC 进程可能在但卡死（等待 API 响应、死循环、内存耗尽等）。进程存在≠正常工作。jsonl 文件的 mtime 是 CC 最近一次有活动的真实信号——CC 每次生成回复都会写入 jsonl。

阈值 600 秒（10分钟）的原因：
- CC 单轮执行可能需要 2-5 分钟（分析日志、修改配置、重启容器）
- 10 分钟足够区分"正在执行"和"卡死了"
- 太短（如 3 分钟）→ 误判频繁 → 不必要重启
- 太长（如 30 分钟）→ 恢复太慢 → 浪费时间

### 为什么 CC 必须在 screen 中运行？

两个原因:
1. **注入唤醒**: `screen -X stuff` 可以向 screen session 中的终端注入文字。这是唤醒卡死 CC 的最轻量方式，不需要杀进程重启。
2. **后台持久**: screen session 不会因为 SSH 断开而终止。CC 可以在后台持续运行。

tmux 也可以（`tmux send-keys`），但 screen 更简单、更通用。

### 为什么 watchdog 和 CC 项目要分开？

CC 的 CronCreate prompt 包含 "git pull + git push"。如果 watchdog 脚本在 CC 的 Git 仓库内，CC 的 git 操作可能会:
- 覆盖 watchdog 的修改（git pull 会 reset）
- 导致 watchdog 脚本本身变化（git push 会提交 watchdog 的日志）

分开后:
- watchdog 目录独立，不受 CC 的 git 操作影响
- CC 项目保持干净，只包含项目本身的代码
- watchdog 的日志和状态文件不会被 CC 误操作

### 为什么需要三层 env vars 保障？

CC v2.1.170+ 的 startup connectivity check 使用 **shell 环境变量**（ANTHROPIC_BASE_URL 等），不读 settings.json。这意味着:
- 直接运行 `claude` → shell env vars 来自当前 shell 环境
- `screen -dmS claude bash -c 'claude ...'` → bash -c 是非交互式，**不 source .bashrc**
- `screen -dmS claude bash --login -c 'claude ...'` → bash --login 会 source .profile → .bashrc

三层保障:
1. **.bashrc**（在 `non-interactive return` 之前设置 env vars）— 普通终端和 screen bash -c 都会 source
2. **.profile**（login shells）— `bash --login` 会 source
3. **restart_claude.sh** 使用 `bash --login` — 确保第三层生效

```bash
# .bashrc 中必须在非交互式退出之前设置 env vars
# 错误做法:
#   [... 各种配置 ...]
#   [[ $- != *i* ]] && return   ← env vars 必须在 return 之前！
#   export ANTHROPIC_BASE_URL=... ← 在 return 之后 = screen bash -c 读不到

# 正确做法:
export ANTHROPIC_BASE_URL=http://127.0.0.1:40001
export ANTHROPIC_API_KEY=sk-litellm-local
# ... 其他 env vars ...
[[ $- != *i* ]] && return   ← env vars 在 return 之前设置
```

### 为什么 `--resume` 恢复上下文？

CC 的 `--resume <session_id>` 可以恢复上次对话的完整上下文，包括:
- 对话历史
- memory 文件
- CLAUDE.md 项目配置

没有 `--resume` → CC 以空白上下文启动 → 不知道自己在做什么 → 需要人重新说明任务
有 `--resume` → CC 恢复到上次中断点 → 知道自己正在做什么 → watchdog 写的 wakeup plan 提供额外指引

### 凌晨静默窗口

某些时段（如 API 配额重置窗口）注入可能导致不必要的操作（配额耗尽时所有请求 429 → CC 反复失败 → 越来越多 error → 又触发更多注入 → 恶性循环）。

在 02:00-04:00 只观察不操作，避免恶性循环。这个时段可以根据你的 API 配额重置时间调整。

## 扩展指南

### 添加自定义 health 检查

编辑 `config.env` 中的 `HEALTH_ENDPOINTS` 数组:

```bash
HEALTH_ENDPOINTS=(
    "proxy:http://127.0.0.1:40001/health"
    "backend1:http://127.0.0.1:41001/health/liveliness"
    "backend2:http://127.0.0.1:42001/health/liveliness"
    "your_new_service:http://127.0.0.1:8080/status"
)
```

### 添加不可变约束

编辑 `config.env` 中的 `IMMUTABLE_CONSTRAINTS` 数组，然后在 `fix_infra.sh` 的 `hard_lint()` 函数中添加对应的检查逻辑。

### 修改唤醒 prompt

`wake_claude.sh` 写的 wakeup plan 是模板。修改 `PLAN_FILE` 的 cat 内容来定制 CC 醒来后的行为。

### 修改 CronCreate prompt

CronCreate 的 prompt 是 CC 内循环的核心逻辑。修改 `config.env` 的 `CC_CRON_PROMPT` 来定义每轮自动执行什么。

### 跨机器协作

本项目原版是两台机器互相修复（opc_uname ↔ opc2_uname）。要实现类似模式:
1. 两台机器各部署 auto-loop
2. 共用一个 Git 仓库
3. CronCreate prompt 中包含 "分析另一台机器的变更 → 为它做优化"
4. watchdog 只管本地机器的 CC 进程和基础设施