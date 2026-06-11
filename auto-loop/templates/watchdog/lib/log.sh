#!/usr/bin/env bash
# log.sh — 统一日志格式库
# 所有 watchdog 脚本 source 此文件以获得统一日志函数
#
# 用法: source "${SCRIPT_DIR}/lib/log.sh"
#
# 日志同时写入:
#   1. watchdog.log — 永久记录
#   2. cron.log — 与 crontab 输出合并
#
# 日志格式: timestamp | pid | prefix | level | message | extra
#
# 提供函数: wd_log / wd_warn / wd_err / wd_ok / wd_act / wd_dry_run

# 路径从 config.env 读取，或使用默认值
WD_LOG_DIR="${WD_LOG_DIR:-${WATCHDOG_DIR}/logs}"
WD_LOG_FILE="${WD_LOG_FILE:-${WD_LOG_DIR}/watchdog.log}"
WD_LOG_PREFIX="${WD_LOG_PREFIX:-watchdog}"

_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
_pid() { echo "$$"; }

_wd_write() {
  local level="$1"; shift
  local msg="$1"; shift
  local extra="$*"
  local line
  line="$(_ts) | $(_pid) | ${WD_LOG_PREFIX} | ${level} | ${msg}"
  if [ -n "${extra}" ]; then
    line="${line} | ${extra}"
  fi
  mkdir -p "${WD_LOG_DIR}"
  echo "${line}" >> "${WD_LOG_FILE}"
  echo "${line}" >> "${WD_LOG_DIR}/cron.log"
}

wd_log()  { _wd_write "INFO"  "$@"; }
wd_warn() { _wd_write "WARN"  "$@"; }
wd_err()  { _wd_write "ERROR" "$@"; }
wd_ok()   { _wd_write "OK"    "$@"; }
wd_act()  { _wd_write "ACTION" "$@"; }

# 干跑模式: WD_DRY_RUN=1 时打印但不执行副作用
wd_dry_run() {
  if [ "${WD_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY-RUN] $*"
    return 0
  fi
  return 1
}