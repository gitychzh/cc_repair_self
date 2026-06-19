# Plan: 持久化 cron-optimization-loop（每 30 分钟自优化）

## 背景
memory `cron-optimization-loop` 记录了「每 10 分钟自优化」机制（读 `configs/NEXT_ROUND.md` 接力 → 分析日志 → 改配置 → push → 写接力），但目前**没有任何持久调度入口**（不在 crontab/systemd/cron.d）。之前的 loop 靠 session 内 `ScheduleWakeup`，session 死即停。

用户决定：频率 **30 分钟**（48 轮/天，与 monitor.sh 同节奏，覆盖 ~15min burst 窗口），headless agent 给 **--dangerously-skip-permissions**。

## 方案：系统级 cron + wrapper 脚本

新增 `scripts/run_optimization_loop.sh`（wrapper）+ 一条 crontab 条目。不放进 systemd（cron 已是本机 CC 基础设施的标准载体：monitor.sh / ts_keepalive.sh 都在 crontab）。

### 1. wrapper 脚本 `scripts/run_optimization_loop.sh`

职责：单实例锁 + git pull + 唤起 headless claude + 超时兜底 + 日志。

```bash
#!/usr/bin/env bash
# 持久化自优化 loop：cron 每 30 分钟唤起一个 headless agent 执行一轮。
# 机制见 memory/cron-optimization-loop.md（NEXT_ROUND.md 接力）
set -uo pipefail

REPO="/home/opc2_uname/cc_ps/cc_repair_self"
WORK_DIR="$REPO"
LOG_FILE="$REPO/logs/optimization_loop.log"
LOCK_FILE="$REPO/.run_optimization_loop.lock"
CLAUDE_BIN="${CLAUDE_BIN:-/home/opc2_uname/.npm-global/bin/claude}"
ROUND_TIMEOUT="${ROUND_TIMEOUT:-1500}"   # 25 分钟（< 30 分钟 cron 间隔，防跨轮重叠）

mkdir -p "$REPO/logs"
touch "$LOG_FILE"
# 自动截断日志（>50KB 保留最后 100 行）
[ -f "$LOG_FILE" ] && [ "$(wc -c <"$LOG_FILE")" -gt 50000 ] && \
    tail -100 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"

ts()  { date -Iseconds; }
log() { echo "$(ts) [$1] $2" >>"$LOG_FILE"; }

# 单实例锁（防 cron 重叠）
exec 9>"$LOCK_FILE" || { log "FATAL" "cannot lock"; exit 2; }
flock -n 9 || { log "SKIP" "another instance running"; exit 0; }

# 健康前置：proxy 不健康就跳过本轮（避免在故障期瞎改配置）
if ! curl -sf -m 5 http://127.0.0.1:40005/health >/dev/null 2>&1; then
    log "SKIP" "cc-proxy 40005 unhealthy, skip this round (monitor.sh 会修)"
    exit 0
fi

cd "$REPO" || { log "FATAL" "cannot cd"; exit 2; }
# 拉接力（可能 opc_uname 也 push 了更新）
git pull --rebase --autostash >>"$LOG_FILE" 2>&1 || log "WARN" "git pull failed, continue anyway"

log "INFO" "round start (timeout ${ROUND_TIMEOUT}s)"
# headless agent：读 NEXT_ROUND.md 流程即在其项目指令(CLAUDE.md)+memory 里，
# 只需给一句 trigger prompt，agent 会按接力机制执行 4 步流程。
timeout --preserve-status -s KILL "$ROUND_TIMEOUT" \
    "$CLAUDE_BIN" -p \
        --dangerously-skip-permissions \
        --add-dir "$REPO" \
        "执行一轮 cc_repair 自优化：按 memory/cron-optimization-loop 流程，读 configs/NEXT_ROUND.md 接力，采集 docker logs 与 quota 数据，有数据支撑才改配置/部署/push，写回接力文件。无数据就只更新接力。务必在 25 分钟内完成。" \
    >>"$LOG_FILE" 2>&1
RC=$?
log "INFO" "round end rc=$RC"
exit 0
```

要点：
- **单实例锁**：`flock -n`，防上一轮没跑完 cron 又起一轮（和 monitor.sh 同模式）。
- **25 分钟超时**：`timeout … KILL`，严格 < 30 分钟 cron 间隔，避免跨轮重叠。
- **健康前置门**：proxy 不健康就跳过（交给 monitor.sh 修复），不瞎改配置。
- **git pull --rebase --autostash**：拉取 opc_uname 可能 push 的更新（接力机制本质是多机协作）。
- **日志自截断**：>50KB 保留尾 100 行（和 ts_keepalive.sh 同）。

### 2. crontab 条目

```cron
*/30 * * * * /home/opc2_uname/cc_ps/cc_repair_self/scripts/run_optimization_loop.sh >/dev/null 2>&1 # cc-self-opt-loop
```
（追加到现有 crontab，与 monitor.sh / ts_keepalive.sh 并列）

### 3. 文档同步

- `CLAUDE.md`：在「关键文件路径与重启」表后补一行说明持久 loop 入口。
- `memory/cron-optimization-loop.md`：更新机制段，记录已挂载到 `*/30 crontab`（不再悬空）。
- `scripts/check_quota_balance.sh`：已存在，不动。

## 执行步骤
1. `scripts/backup_config.sh`（按协议先备份）
2. 写 `scripts/run_optimization_loop.sh` + `chmod +x`
3. `crontab -l | … ` 追加新条目（保留现有 4 条 + tailscale-monitor 不动）
4. 手动跑一次 wrapper 验证：`bash scripts/run_optimization_loop.sh` 看 logs/optimization_loop.log
5. 更新 CLAUDE.md + memory（cron-optimization-loop）
6. `git add … && git commit && git push`

## 风险与缓解
- **quota 消耗**：48 轮/天 headless agent 会吃 quota，但每轮只读+少量改动，throttle 2s 已限速；接力机制让「无数据→不改」。用户已知接受。
- **无人值守改配置**：`--dangerously-skip-permissions` 让 agent 可 push/restart。缓解：wrapper 加 proxy 健康门、25 分钟超时、单实例锁；agent 受 CLAUDE.md「NEVER CHANGE」约束 + 「有数据才改」原则。
- **多机冲突**：两台机器都跑 loop 会抢 push。缓解：git pull --rebase --autostash；如后续 opc_uname 也启用，建议错开调度（如本机 `*/30`，对机 `*/30 offset 15min`）。
- **agent 跑飞**：25 分钟 KILL 兜底 + 单实例锁。

## 验证
- `crontab -l` 显示新条目
- 手动 `bash scripts/run_optimization_loop.sh` 后 `tail logs/optimization_loop.log` 出现 round start/end
- 等 30 分钟后 crontab 自动触发，日志新增一轮（证明持久化生效）
