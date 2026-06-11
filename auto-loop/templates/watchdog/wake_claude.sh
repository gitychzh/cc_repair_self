#!/usr/bin/env bash
# wake_claude.sh — 双保险唤醒: screen 注入 → 90s 回检 → restart + --resume
#
# 唤醒策略:
#   阶段1: 通过 screen -X stuff 向 CC session 注入 "继续" 文字
#          → 等待 RETRY_SECONDS 看 jsonl mtime 是否变化
#          → 如果有变化 → CC 已恢复 → exit 0
#   阶段2: 注入无效 → 杀进程+杀screen → 启动新 screen + --resume
#          → 写 wakeup plan 到 ~/.claude/plans/
#          → CC 醒来后立刻知道要做什么
#
# 用法: wake_claude.sh <latest_session_id>
# exit 0: 注入成功 或 cooldown 跳过
# exit 2: 注入失败，已走 restart 分支
# exit 3: restart 也失败
# exit 4: 硬约束被破坏（无 session ID）

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lib/log.sh"

# 加载配置
CONFIG_ENV="${SCRIPT_DIR}/../config.env"
if [ -f "${CONFIG_ENV}" ]; then
  source "${CONFIG_ENV}"
fi

LATEST_SESSION="${1:-}"
RETRY_SECONDS="${RETRY_SECONDS:-90}"
SCREEN_NAME="${SCREEN_NAME:-claude}"
PROJECT_DIR="${PROJECT_DIR:-${HOME}/your-project-dir}"
SESSION_DIR="${CC_SESSION_DIR:-${HOME}/.claude/projects/-home-your-user-your-project-dir}"
STATE_DIR="${WATCHDOG_DIR:-${HOME}/your-watchdog-dir}/state"
CLAUDE_BIN="${CLAUDE_BIN:-${HOME}/.npm-global/bin/claude}"
CC_PERMISSION_MODE="${CC_PERMISSION_MODE:-bypassPermissions}"

if [ -z "${LATEST_SESSION}" ]; then
  wd_err "wake_no_session_id"
  echo '{"result":"error","reason":"no_session_id"}'
  exit 4
fi

# ── 防抖动: COOLDOWN_SEC 内不重复唤醒 ──
STATE_FILE="${STATE_DIR}/last_wakeup.json"
COOLDOWN_SEC="${WAKE_COOLDOWN_SEC:-300}"
if [ -f "${STATE_FILE}" ]; then
  last_ts=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ts",0))' "${STATE_FILE}" 2>/dev/null || echo 0)
  if [ "${last_ts}" -gt 0 ]; then
    now=$(date +%s)
    delta=$(( now - last_ts ))
    if [ "${delta}" -lt "${COOLDOWN_SEC}" ]; then
      wd_log "wake_cooldown" "delta=${delta}s < ${COOLDOWN_SEC}s, skip"
      echo "{\"result\":\"cooldown\",\"delta_sec\":${delta}}"
      exit 0
    fi
  fi
fi

mkdir -p "${STATE_DIR}"

write_state() {
  local result="$1" method="$2" extra="$3"
  python3 - "$result" "$method" "$extra" "$(date +%s)" "${LATEST_SESSION}" <<'PY' > "${STATE_FILE}"
import json, sys
out = {
    "ts": int(sys.argv[4]),
    "result": sys.argv[1],
    "method": sys.argv[2],
    "detail": sys.argv[3],
    "session_id": sys.argv[5],
}
print(json.dumps(out, ensure_ascii=False))
PY
}

session_path="${SESSION_DIR}/${LATEST_SESSION}.jsonl"
mtime_before=0
if [ -f "${session_path}" ]; then
  mtime_before=$(stat -c %Y "${session_path}" 2>/dev/null || echo 0)
fi

# ── 阶段 1: screen 注入 ──
if screen -ls 2>/dev/null | grep -q "\.${SCREEN_NAME}"; then
  wd_act "wake_inject" "session=${LATEST_SESSION}" "screen=${SCREEN_NAME}"
  if [ "${WD_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY-RUN] screen -S ${SCREEN_NAME} -X stuff \$'\\n继续\\n'"
  else
    screen -S "${SCREEN_NAME}" -X stuff $'\n继续\n' 2>&1 || wd_warn "screen_stuff_failed"
  fi
  # 90s 回检: 每 10s 检查一次 jsonl mtime
  slept=0
  moved=0
  while [ "${slept}" -lt "${RETRY_SECONDS}" ]; do
    sleep 10
    slept=$(( slept + 10 ))
    if [ -f "${session_path}" ]; then
      mtime_after=$(stat -c %Y "${session_path}" 2>/dev/null || echo 0)
      if [ "${mtime_after}" -gt "${mtime_before}" ]; then
        moved=1
        break
      fi
    fi
  done
  if [ "${moved}" = "1" ]; then
    wd_ok "wake_inject_ok" "slept=${slept}s" "mtime_delta=$(( $(stat -c %Y "${session_path}") - mtime_before ))s"
    write_state "ok" "inject" "slept=${slept}s"
    echo '{"result":"ok","method":"inject","slept_sec":'${slept}'}'
    exit 0
  fi
  wd_warn "wake_inject_no_progress" "slept=${slept}s" "mtime_unchanged"
else
  wd_log "wake_no_screen" "screen=${SCREEN_NAME} not present, jumping to restart"
fi

# ── 阶段 2: kill + restart + --resume ──

# 写 auto-wakeup plan，让 CC 醒来立刻知道要做什么
PLAN_DIR="${HOME}/.claude/plans"
mkdir -p "${PLAN_DIR}"
PLAN_FILE="${PLAN_DIR}/$(date +%Y%m%d-%H%M%S)-auto-wakeup.md"
cat > "${PLAN_FILE}" <<PLANEOF
# Auto Wakeup Context ($(date '+%F %T %z'))

watchdog 判定 CC session 已静默 ≥ ${STALL_THRESHOLD_SEC:-600} 秒，注入未生效，已执行 restart 分支。

## 当前 session
- `${LATEST_SESSION}`
- 路径: `${session_path}`

## 醒来第一件事
1. 查看最近巡检记录: `tail -30 ${WATCHDOG_DIR:-${HOME}/your-watchdog-dir}/logs/watchdog.log`
2. 检查基础设施健康: `bash ${PROJECT_DIR}/scripts/health_check.sh`
3. 查看之前的工作: `ls -lt ${PROJECT_DIR}/logs/`
4. 接着上一次的 in_progress 任务继续
PLANEOF
wd_log "wake_plan_written" "file=${PLAN_FILE}"

# 杀现有 CC 进程
PIDS=$(pgrep -f 'claude --permission-mode' 2>/dev/null || true)
if [ -n "${PIDS}" ]; then
  wd_act "wake_kill_existing" "pids=${PIDS}"
  for pid in ${PIDS}; do
    if [ "${WD_DRY_RUN:-0}" = "1" ]; then
      echo "[DRY-RUN] kill ${pid}"
    else
      kill "${pid}" 2>/dev/null || true
    fi
  done
  sleep 5
  # 顽固进程 → SIGKILL
  PIDS=$(pgrep -f 'claude --permission-mode' 2>/dev/null || true)
  if [ -n "${PIDS}" ]; then
    for pid in ${PIDS}; do
      if [ "${WD_DRY_RUN:-0}" = "1" ]; then
        echo "[DRY-RUN] kill -9 ${pid}"
      else
        kill -9 "${pid}" 2>/dev/null || true
      fi
    done
    sleep 2
  fi
fi

# 杀 screen
if [ "${WD_DRY_RUN:-0}" != "1" ]; then
  screen -S "${SCREEN_NAME}" -X quit 2>/dev/null || true
  sleep 1
fi

# 启动新 screen with --resume
wd_act "wake_restart" "session=${LATEST_SESSION}" "plan=${PLAN_FILE}"
if [ "${WD_DRY_RUN:-0}" = "1" ]; then
  echo "[DRY-RUN] screen -dmS ${SCREEN_NAME} bash --login -c '${CLAUDE_BIN} --permission-mode ${CC_PERMISSION_MODE} --resume ${LATEST_SESSION} ...'"
else
  # bash --login 确保 .profile → .bashrc 被加载（env vars 可用）
  screen -dmS "${SCREEN_NAME}" bash --login -c "${CLAUDE_BIN} --permission-mode ${CC_PERMISSION_MODE} --resume ${LATEST_SESSION} 2>&1 | tee -a ${PROJECT_DIR}/claude_output.log; echo '=== claude exited, sleeping 3600 ==='; sleep 3600"
fi

sleep 5

if [ "${WD_DRY_RUN:-0}" = "1" ]; then
  echo '{"result":"dry_run","method":"restart"}'
  write_state "dry_run" "restart" ""
  exit 0
fi

# 验证: CC 进程 + screen 是否在
sleep 5
new_pid=$(pgrep -f 'claude --permission-mode' | head -1 || true)
if [ -n "${new_pid}" ]; then
  wd_ok "wake_restart_ok" "pid=${new_pid}" "session=${LATEST_SESSION}"
  write_state "ok" "restart" "pid=${new_pid}"
  echo "{\"result\":\"ok\",\"method\":\"restart\",\"pid\":\"${new_pid}\"}"
  exit 0
fi

wd_err "wake_restart_failed" "no claude pid after restart"
write_state "fail" "restart" "no_pid"
echo '{"result":"fail","method":"restart","reason":"no_pid"}'
exit 3