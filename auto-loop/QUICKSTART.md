# QUICKSTART.md — 5 分钟部署指南

## 前提条件

- Linux 主机（Ubuntu/Debian 推荐）
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装（`npm install -g @anthropic-ai/claude-code`）
- CC 连接的 API proxy/backend 已部署并可访问
- `screen`、`pgrep`、`python3`、`jq` 已安装
- 有 sudo 权限（如果需要修改系统级配置）

## 步骤 1: 获取 auto-loop

```bash
# 假设你已经有了一个 Git 项目目录
cd ~/your-project

# 把 auto-loop 目录复制到你的项目中（或独立放置）
cp -r /path/to/auto-loop/ ./auto-loop/
```

## 步骤 2: 配置

```bash
cd auto-loop

# 复制配置模板
cp config.env.template config.env

# 编辑配置（最重要的步骤！）
vi config.env
```

**必填字段**:
- `PROJECT_DIR` — 你的 CC 工作目录（Git 仓库路径）
- `CC_SESSION_DIR` — CC session 存储路径（先启动一次 CC，然后 `ls ~/.claude/projects/` 找到）
- `WATCHDOG_DIR` — watchdog 项目目录（与 CC 项目分开）
- `DEPLOY_DIR` — 基础设施配置部署目录
- `HEALTH_ENDPOINTS` — 需要监控的端点列表
- `EXPECTED_CONTAINERS` — 需要监控的 Docker 容器列表

**如果你没有 Docker/容器**: 把 `HEALTH_ENDPOINTS` 和 `EXPECTED_CONTAINERS` 设为空数组，`MIN_CONTAINERS` 设为 0。watchdog 会跳过容器检查。

## 步骤 3: 配置 CC settings

```bash
# 复制 CC settings 模板
cp templates/cc-session/settings.json.template ~/.claude/settings.json

# 编辑 settings（替换所有 <PLACEHOLDER>）
vi ~/.claude/settings.json
```

**关键配置**:
- `env.ANTHROPIC_BASE_URL` — 你的 API proxy 地址
- `env.ANTHROPIC_API_KEY` — API key
- `permissions.allow` — 全开放（无人值守需要）
- `defaultMode` — `bypassPermissions`
- `skipDangerousModePermissionPrompt` — `true`
- `model` — CC 使用的前端模型名

## 步骤 4: 设置 shell env vars

CC v2.1.170+ 的启动连通性检查使用 shell 环境变量，不是 settings.json。

```bash
# 在 ~/.bashrc 中（在 [[ $- != *i* ]] && return 之前）添加:
export ANTHROPIC_BASE_URL="http://127.0.0.1:40001"
export ANTHROPIC_API_KEY="sk-litellm-local"
export HTTPS_PROXY="http://127.0.0.1:7880"  # 如果需要代理
export HTTP_PROXY="http://127.0.0.1:7880"
export NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="155000"

# 在 ~/.profile 中也添加同样的内容（确保 login shell 有这些变量）
```

⚠️ **关键**: env vars 必须在 `.bashrc` 的 `[[ $- != *i* ]] && return` 之前设置，否则 screen 的 `bash -c` 读不到。

## 步骤 5: 一键部署

```bash
cd auto-loop

# 部署（交互式，会提示你确认）
bash deploy.sh

# 或者只看会做什么，不实际执行
bash deploy.sh --dry-run
```

deploy.sh 会:
1. 创建 watchdog 目录结构
2. 复制脚本并 chmod +x
3. 安装 crontab 条目
4. 设置 CC 辅助脚本

## 步骤 6: 启动 CC session

```bash
# 启动 CC（在 screen 中）
bash ~/your-project/scripts/start.sh

# 或者手动启动:
screen -dmS claude bash --login -c "claude --permission-mode bypassPermissions 2>&1 | tee ~/your-project/claude_output.log"
```

## 步骤 7: 注册 CronCreate

进入 CC session:
```bash
screen -r claude
```

等待 CC 就绪，然后告诉 CC:
```
请帮我注册一个 CronCreate 定时任务，每 10 分钟执行一次，
durable 为 true，prompt 为:
"检查远程仓库是否有更新：git pull origin main。
如果有新更新，分析变更内容，对比本机配置进行优化。
所有参数修改必须有日志数据支撑。
优化后更新部署状态文档并 push 到远程仓库。
如果没有更新，只做日志数据分析，有数据支撑才修改参数。"
``

CC 会调用 CronCreate 工具注册任务。之后每 10 分钟 CC 会自动执行一轮优化。

## 步骤 8: 验证

```bash
# 检 watchdog 是否正常
tail -5 ~/your-watchdog-dir/logs/watchdog.log

# 应该看到类似:
# 2026-06-11T22:00:01 | ... | watchdog | INFO | cycle_normal | no action

# 检 CC 是否在
screen -list | grep claude

# 检 CronCreate 是否注册
cat ~/.claude/projects/-your-project-path/.claude/scheduled_tasks.json
```

## 日常操作

| 操作 | 命令 |
|------|------|
| 进入 CC session | `screen -r claude` |
| 退出 CC session（不停止） | `Ctrl+A, D` |
| 查看 watchdog 日志 | `tail -f ~/your-watchdog-dir/logs/watchdog.log` |
| 查看 CC 输出日志 | `tail -f ~/your-project/claude_output.log` |
| 手动触发 CC 一轮 | 进入 screen，向 CC 发送 prompt |
| 重启 CC | `bash ~/your-project/scripts/restart_claude.sh` |
| 暂停 watchdog | `bash ~/your-watchdog-dir/scripts/install.sh uninstall` |
| 恢复 watchdog | `bash auto-loop/deploy.sh` |

## 常见问题

### CC 启动失败（401 error）

原因: shell env vars 没设置正确。CC 的启动连通性检查用 shell 环境变量，不是 settings.json。

解决:
```bash
# 检查 env vars 是否在 login shell 中可用
bash --login -c 'echo $ANTHROPIC_BASE_URL'
# 应输出你的 proxy URL

# 如果为空，检查 .bashrc 和 .profile 中 env vars 的位置
grep ANTHROPIC_BASE_URL ~/.bashrc ~/.profile
# 必须在 [[ $- != *i* ]] && return 之前
```

### CC 总是被 watchdog 重启

原因: CC 的 CronCreate prompt 执行时间太长，超过 STALL_THRESHOLD_SEC（默认 600s = 10分钟）。

解决:
1. 增大 `STALL_THRESHOLD_SEC`（如 1800 = 30分钟）
2. 简化 CronCreate prompt（减少每轮工作量）
3. 检查 CC 是否真的在执行（看 `claude_output.log`）

### Watchdog 报 infra_ok=false

原因: 基础设施端点不可达或容器数不足。

解决:
```bash
# 手动检查端点
curl -sf http://127.0.0.1:40001/health && echo "OK" || echo "DOWN"

# 检查容器
docker ps --filter 'health=healthy' --format '{{.Names}} {{.Status}}'

# 手动修复
bash ~/your-watchdog-dir/scripts/fix_infra.sh
```

### screen 注入唤醒无效

原因: CC 卡死太深，不响应终端输入。

解决: watchdog 会自动 fallback 到重启。如果重启也失败:
```bash
# 手动重启
bash ~/your-project/scripts/restart_claude.sh

# 查看 CC 进程
pgrep -fa claude
```