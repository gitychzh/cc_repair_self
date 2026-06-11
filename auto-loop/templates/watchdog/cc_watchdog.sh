#!/usr/bin/env bash
# cc_watchdog.sh — 主入口（被 cron 调用）
#
# 行为:
#   1. 判定 CC session 是否卡死（detect_stall）
#   2. 拍健康快照（health_snapshot）
#   3. 按决策矩阵行动:
#      - 没卡死 + 健康 → 记 log 退出
#      - 卡死 + 健康 → 唤醒（inject → 90s → restart）
#      - 卡死 + 不健康 → fix_infra → 唤醒
#      - 不卡死 + 不健康 → fix_infra（不影响当前工作）
#
# 决策矩阵:
# ┌─────────────────┬─────────────────┬─────────────────────────────────┐
# │ stalled         │ infra           │ action                          │
# ├─────────────────┼─────────────────┼─────────────────────────────────┤
# │ false           │ ok              │ normal — 退出                   │
# │ false           │ broken          │ fix_infra（不影响当前工作）     │
# │ true            │ ok              │ wake（inject / restart）        │
# │ true            │ broken          │ fix_infra → wake                │
# └─────────────────┴─────────────────┴─────────────────────────────────┘
#
# 单次执行时间 ≤ 3 分钟（cron 不会等更久）

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lib/log.sh"

# 加载配置
CONFIG_ENV="${SCRIPT_DIR}/../config.env"
if [ -f "${CONFIG_ENV}" ]; then
  source "${CONFIG_ENV}"
fi

WD_DRY_RUN="${WD_DRY_RUN:-0}"
START_TS=$(date +%s)

# 凌晨静默窗口
HOUR=$(date +%H)
PAUSE_WINDOW=false
if [ "${HOUR}" -ge "${QUIET_HOUR_START:-2}" ] && [ "${HOUR}" -lt "${QUIET_HOUR_END:-4}" ]; then
  PAUSE_WINDOW=true
fi

wd_log "cycle_start" "dry_run=${WD_DRY_RUN}" "pause_window=${PAUSE_WINDOW}"

# ── 1. detect_stall ──
DETECT_OUT=$(${SCRIPT_DIR}/detect_stall.sh 2>/dev/null || echo '{"stalled":false,"reason":"detect_failed"}')
STALLED=$(echo "${DETECT_OUT}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("stalled",False))' 2>/dev/null || echo "False")
LATEST_SESSION=$(echo "${DETECT_OUT}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("latest_session",""))' 2>/dev/null || echo "")
MTIME_AGE=$(echo "${DETECT_OUT}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("mtime_age_sec",0))' 2>/dev/null || echo "0")

# ── 2. health_snapshot ──
HEALTH_OUT=$(${SCRIPT_DIR}/health_snapshot.sh 2>/dev/null || echo '{"infra_ok":false,"claude_alive":false}')
INFRA_OK=$(echo "${HEALTH_OUT}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("infra_ok",False))' 2>/dev/null || echo "False")
CLAUDE_ALIVE=$(echo "${HEALTH_OUT}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("claude_alive",False))' 2>/dev/null || echo "False")

wd_log "cycle_decide" "stalled=${STALLED}" "infra_ok=${INFRA_OK}" "claude_alive=${CLAUDE_ALIVE}" "mtime_age=${MTIME_AGE}" "session=${LATEST_SESSION}"

# ── 3. 决策矩阵 ──

if [ "${STALLED}" = "False" ] && [ "${INFRA_OK}" = "True" ]; then
  wd_log "cycle_normal" "no action"
  echo '{"result":"normal"}'
  exit 0
fi

wd_warn "cycle_abnormal" "stalled=${STALLED}" "infra_ok=${INFRA_OK}" "claude_alive=${CLAUDE_ALIVE}"

# 凌晨窗口只观察不注入
if [ "${STALLED}" = "True" ] && [ "${PAUSE_WINDOW}" = "true" ]; then
  wd_log "cycle_pause_window" "stalled=true but in ${QUIET_HOUR_START:-2}-${QUIET_HOUR_END:-4} window, observe only"
  echo '{"result":"pause_window_observe"}'
  exit 0
fi

# 3a. 容器/端点不健康 → 优先 fix_infra
if [ "${INFRA_OK}" != "True" ]; then
  wd_act "cycle_fix_infra" "reason=infra_unhealthy"
  WD_LOG_DIR="${WD_LOG_DIR}" WD_DRY_RUN="${WD_DRY_RUN}" \
    bash ${SCRIPT_DIR}/fix_infra.sh
  FIX_RC=$?
  # 重新拍快照
  HEALTH_OUT=$(${SCRIPT_DIR}/health_snapshot.sh 2>/dev/null || echo '{"infra_ok":false}')
  INFRA_OK=$(echo "${HEALTH_OUT}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("infra_ok",False))' 2>/dev/null || echo "False")
  wd_log "cycle_fix_infra_done" "rc=${FIX_RC}" "infra_ok_after=${INFRA_OK}"
fi

# 3b. 卡死 → 唤醒
if [ "${STALLED}" = "True" ] && [ -n "${LATEST_SESSION}" ]; then
  wd_act "cycle_wake" "session=${LATEST_SESSION}" "infra_ok=${INFRA_OK}" "claude_alive=${CLAUDE_ALIVE}"
  WAKE_OUT=$(${SCRIPT_DIR}/wake_claude.sh "${LATEST_SESSION}" 2>&1)
  WAKE_RC=$?
  wd_log "cycle_wake_done" "rc=${WAKE_RC}" "out=${WAKE_OUT}"
fi

# ── 4. 收尾 ──
END_TS=$(date +%s)
ELAPSED=$(( END_TS - START_TS ))
wd_log "cycle_end" "elapsed_sec=${ELAPSED}"

if [ "${ELAPSED}" -gt 180 ]; then
  wd_warn "cycle_too_slow" "elapsed=${ELAPSED}s > 180s, cron may overlap next run"
fi

# 唤醒后仍不健康 → 升级 fix_infra
if [ "${STALLED}" = "True" ]; then
  HEALTH_OUT=$(${SCRIPT_DIR}/health_snapshot.sh 2>/dev/null || echo '{"infra_ok":false}')
  INFRA_OK2=$(echo "${HEALTH_OUT}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("infra_ok",False))' 2>/dev/null || echo "False")
  if [ "${INFRA_OK2}" != "True" ]; then
    wd_act "cycle_escalate_fix_infra" "reason=wake_failed_and_unhealthy"
    bash ${SCRIPT_DIR}/fix_infra.sh
  fi
fi

echo "{\"result\":\"handled\",\"elapsed_sec\":${ELAPSED}}"
exit 0