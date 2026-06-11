#!/usr/bin/env bash
# install.sh — 一键安装 cc_watchdog
#
# 功能:
#   - 创建日志/状态目录
#   - chmod +x 所有脚本
#   - 注入 crontab 条目（自动去重）
#   - 不动 docker、不动 deploy dir
#
# 用法:
#   install.sh          # 安装
#   install.sh uninstall  # 卸载（只删除 crontab 条目，保留日志）
#
# 卸载后日志和脚本文件仍在，需手动 rm -rf 删除

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
SCRIPTS_DIR="${PROJECT_DIR}/scripts"
LOGS_DIR="${PROJECT_DIR}/logs"
STATE_DIR="${PROJECT_DIR}/state"
ENTRY="${SCRIPTS_DIR}/cc_watchdog.sh"
CRON_INTERVAL="${WATCHDOG_CRON:-*/15}"
CRON_LINE="${CRON_INTERVAL} * * * * ${ENTRY} >> ${LOGS_DIR}/cron.log 2>&1"
CRON_MARKER="# cc_watchdog auto-managed"

if [ "${1:-}" = "uninstall" ]; then
  echo "=== Uninstalling cc_watchdog ==="
  ( crontab -l 2>/dev/null | grep -v 'cc_watchdog.sh' ) | crontab -
  echo "  crontab entry removed"
  echo "  logs preserved at ${LOGS_DIR}/ (manual rm -rf if needed)"
  exit 0
fi

echo "=== Installing cc_watchdog ==="

# 1. 目录
mkdir -p "${LOGS_DIR}" "${STATE_DIR}"
echo "  logs dir:   ${LOGS_DIR}"
echo "  state dir:  ${STATE_DIR}"

# 2. chmod
chmod +x "${SCRIPTS_DIR}/"*.sh "${SCRIPTS_DIR}/lib/"*.sh
echo "  scripts:    $(ls ${SCRIPTS_DIR}/*.sh | wc -l) executable"

# 3. crontab — 自动去重
EXISTING=$(crontab -l 2>/dev/null || true)
if echo "${EXISTING}" | grep -qF "${ENTRY}"; then
  echo "  crontab:    already installed (no change)"
else
  TMP=$(mktemp)
  if [ -n "${EXISTING}" ]; then
    echo "${EXISTING}" > "${TMP}"
  fi
  echo "${CRON_MARKER}" >> "${TMP}"
  echo "${CRON_LINE}" >> "${TMP}"
  crontab "${TMP}"
  rm -f "${TMP}"
  echo "  crontab:    installed — ${CRON_LINE}"
fi

# 4. 立即 dry-run 一次确认无语法错误
echo ""
echo "=== Dry-run verification ==="
WD_DRY_RUN=1 bash "${ENTRY}"
RC=$?
echo ""
if [ "${RC}" = "0" ]; then
  echo "=== Install OK ==="
  echo ""
  echo "Tail logs with:  tail -f ${LOGS_DIR}/watchdog.log"
  echo "Uninstall with:  bash ${SCRIPT_DIR}/install.sh uninstall"
else
  echo "=== Install FAILED (dry-run rc=${RC}) ==="
  exit 1
fi