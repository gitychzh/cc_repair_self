#!/usr/bin/env bash
# 持久化自优化 loop：cron 每 30 分钟唤起一个 headless agent 执行一轮。
# 机制见 memory/cron-optimization-loop.md（NEXT_ROUND.md 接力）
set -uo pipefail

REPO="/home/opc2_uname/cc_ps/cc_repair_self"
LOG_FILE="$REPO/logs/optimization_loop.log"
LOCK_FILE="$REPO/.run_optimization_loop.lock"
CLAUDE_BIN="${CLAUDE_BIN:-/home/opc2_uname/.npm-global/bin/claude}"
ROUND_TIMEOUT="${ROUND_TIMEOUT:-1500}"   # 25 分钟（< 30 分钟 cron 间隔，防跨轮重叠）

mkdir -p "$REPO/logs" "$REPO/.lockdir" 2>/dev/null
touch "$LOG_FILE"
# 自动截断日志（>50KB 保留最后 100 行）
[ -f "$LOG_FILE" ] && [ "$(wc -c <"$LOG_FILE")" -gt 50000 ] && \
    { tail -100 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"; }

ts()  { date -Iseconds; }
log() { echo "$(ts) [$1] $2" >>"$LOG_FILE"; }

# 单实例锁（防 cron 重叠）
exec 9>"$LOCK_FILE" || { log "FATAL" "cannot open lock"; exit 2; }
flock -n 9 || { log "SKIP" "another instance running"; exit 0; }

# 健康前置门：proxy 不健康就跳过本轮（避免在故障期瞎改配置，交给 monitor.sh 修）
if ! curl -sf -m 5 http://127.0.0.1:40005/health >/dev/null 2>&1; then
    log "SKIP" "cc-proxy 40005 unhealthy, skip this round (monitor.sh will repair)"
    exit 0
fi

cd "$REPO" || { log "FATAL" "cannot cd $REPO"; exit 2; }
# 拉接力（可能 opc_uname 也 push 了更新）
git pull --rebase --autostash >>"$LOG_FILE" 2>&1 || log "WARN" "git pull failed, continue anyway"

log "INFO" "round start (timeout ${ROUND_TIMEOUT}s)"
# headless agent：执行流程在 memory/cron-optimization-loop + CLAUDE.md，此处只发 trigger。
timeout --preserve-status -s KILL "$ROUND_TIMEOUT" \
    "$CLAUDE_BIN" -p \
        --dangerously-skip-permissions \
        --add-dir "$REPO" \
        "执行一轮 cc_repair 自优化：按 memory/cron-optimization-loop 流程，先 git pull 读 configs/NEXT_ROUND.md 接力，采集 docker logs(40001/40002/40005/ms_uni41001) 与 bash scripts/check_quota_balance.sh 数据。有数据支撑才改配置/部署/push，无数据就只更新接力文件。务必在 25 分钟内完成一轮。" \
    >>"$LOG_FILE" 2>&1
RC=$?
log "INFO" "round end rc=$RC"
exit 0
