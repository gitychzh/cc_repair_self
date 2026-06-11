#!/usr/bin/env bash
# deploy.sh — 一键部署 auto-loop 自循环框架
#
# 功能:
#   1. 复制 config.env.template → config.env（首次部署）
#   2. 替换模板中的 <PLACEHOLDER>（如果提供了参数）
#   3. 创建 watchdog 项目目录
#   4. 复制脚本到 watchdog 目录
#   5. chmod +x 所有脚本
#   6. 安装 crontab 条目
#   7. 设置 CC settings.json
#   8. 启动 CC session
#
# 用法:
#   deploy.sh                                # 交互式部署（会提示填写配置）
#   deploy.sh --from-env <path>              # 从已有的 config.env 部署
#   deploy.sh --dry-run                      # 只显示会做什么，不执行
#   deploy.sh --uninstall                    # 卸载（删除 crontab、停止 CC）

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DRY_RUN=false
UNINSTALL=false
FROM_ENV=""
ACTION="deploy"

# 解析参数
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=true; shift ;;
    --from-env)   FROM_ENV="$2"; shift 2 ;;
    --uninstall)  UNINSTALL=true; shift ;;
    *)            echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if $UNINSTALL; then
  echo "=== Uninstalling auto-loop ==="
  # 删除 crontab 条目
  ( crontab -l 2>/dev/null | grep -v 'cc_watchdog.sh' ) | crontab -
  echo "  crontab entry removed"
  # 不删除脚本和日志（可能需要回顾）
  echo "  scripts and logs preserved (manual rm -rf if needed)"
  echo "=== Uninstall complete ==="
  exit 0
fi

# ── 1. config.env ──
CONFIG_ENV="${SCRIPT_DIR}/config.env"
CONFIG_TEMPLATE="${SCRIPT_DIR}/config.env.template"

if [ -n "${FROM_ENV}" ] && [ -f "${FROM_ENV}" ]; then
  cp "${FROM_ENV}" "${CONFIG_ENV}"
  echo "[1] Using existing config.env from: ${FROM_ENV}"
elif [ ! -f "${CONFIG_ENV}" ]; then
  cp "${CONFIG_TEMPLATE}" "${CONFIG_ENV}"
  echo "[1] Created config.env from template"
  echo "    → Please edit ${CONFIG_ENV} and fill in your values before continuing."
  echo "    → Key fields: PROJECT_DIR, CC_SESSION_DIR, DEPLOY_DIR, HEALTH_ENDPOINTS, EXPECTED_CONTAINERS"
  if ! $DRY_RUN; then
    echo ""
    echo "Press Enter to open editor, or Ctrl+C to exit and edit manually..."
    read -r
    ${EDITOR:-vi} "${CONFIG_ENV}"
  fi
else
  echo "[1] config.env already exists (no change)"
fi

# 加载配置
source "${CONFIG_ENV}"

# ── 2. 创建 watchdog 项目 ──
WATCHDOG_SCRIPTS="${WATCHDOG_DIR}/scripts"
WATCHDOG_LIB="${WATCHDOG_SCRIPTS}/lib"
WATCHDOG_LOGS="${WATCHDOG_DIR}/logs"
WATCHDOG_STATE="${WATCHDOG_DIR}/state"

echo "[2] Setting up watchdog directory: ${WATCHDOG_DIR}"

if $DRY_RUN; then
  echo "    [DRY-RUN] mkdir -p ${WATCHDOG_SCRIPTS} ${WATCHDOG_LIB} ${WATCHDOG_LOGS} ${WATCHDOG_STATE}"
else
  mkdir -p "${WATCHDOG_SCRIPTS}" "${WATCHDOG_LIB}" "${WATCHDOG_LOGS}" "${WATCHDOG_STATE}"
fi

# 复制脚本
TEMPLATE_WATCHDOG="${SCRIPT_DIR}/templates/watchdog"
for f in "${TEMPLATE_WATCHDOG}/"*.sh "${TEMPLATE_WATCHDOG}/lib/"*.sh; do
  basename=$(basename "$f")
  dest="${WATCHDOG_SCRIPTS}/${basename}"
  if [ "$(dirname "$f")" = "${TEMPLATE_WATCHDOG}/lib" ]; then
    dest="${WATCHDOG_LIB}/${basename}"
  fi
  if $DRY_RUN; then
    echo "    [DRY-RUN] cp $f → $dest"
  else
    cp "$f" "$dest"
    chmod +x "$dest"
  fi
done

echo "    scripts copied and chmod +x"

# ── 3. 安装 watchdog crontab ──
echo "[3] Installing watchdog crontab..."
CRON_INTERVAL="${WATCHDOG_CRON:-*/15}"
ENTRY="${WATCHDOG_SCRIPTS}/cc_watchdog.sh"
CRON_LINE="${CRON_INTERVAL} * * * * ${ENTRY} >> ${WATCHDOG_LOGS}/cron.log 2>&1"

if $DRY_RUN; then
  echo "    [DRY-RUN] crontab line: ${CRON_LINE}"
else
  EXISTING=$(crontab -l 2>/dev/null || true)
  if echo "${EXISTING}" | grep -qF "${ENTRY}"; then
    echo "    already installed (no change)"
  else
    TMP=$(mktemp)
    echo "${EXISTING}" > "${TMP}"
    echo "# cc_watchdog auto-managed" >> "${TMP}"
    echo "${CRON_LINE}" >> "${TMP}"
    crontab "${TMP}"
    rm -f "${TMP}"
    echo "    installed — ${CRON_LINE}"
  fi
fi

# ── 4. 设置 CC settings ──
echo "[4] Setting up CC settings.json..."
SETTINGS_TEMPLATE="${SCRIPT_DIR}/templates/cc-session/settings.json.template"
SETTINGS_DEST="${HOME}/.claude/settings.json"

if $DRY_RUN; then
  echo "    [DRY-RUN] would configure ${SETTINGS_DEST}"
else
  if [ ! -f "${SETTINGS_DEST}" ]; then
    echo "    No existing settings.json — you need to create one from the template:"
    echo "    cp ${SETTINGS_TEMPLATE} ${SETTINGS_DEST}"
    echo "    Then edit it to fill in your <PLACEHOLDER> values."
  else
    echo "    Existing settings.json found — preserving (manual edit if needed)"
    echo "    Key fields to verify: env.ANTHROPIC_BASE_URL, permissions, defaultMode, model"
  fi
fi

# ── 5. 复制辅助脚本 ──
echo "[5] Setting up CC session scripts..."

CC_SCRIPTS="${PROJECT_DIR}/scripts"
if $DRY_RUN; then
  echo "    [DRY-RUN] copy restart_claude.sh + start.sh + statusline-command.sh"
else
  mkdir -p "${CC_SCRIPTS}"

  cp "${SCRIPT_DIR}/templates/cc-session/restart_claude.sh" "${CC_SCRIPTS}/"
  cp "${SCRIPT_DIR}/templates/cc-session/start.sh" "${CC_SCRIPTS}/"
  chmod +x "${CC_SCRIPTS}/restart_claude.sh" "${CC_SCRIPTS}/start.sh"

  # statusline-command.sh
  cp "${SCRIPT_DIR}/templates/cc-session/statusline-command.sh" "${HOME}/.claude/statusline-command.sh"
  chmod +x "${HOME}/.claude/statusline-command.sh"
fi

# ── 6. 验证 ──
echo ""
echo "[6] Dry-run verification..."
WD_DRY_RUN=1 bash "${WATCHDOG_SCRIPTS}/cc_watchdog.sh" 2>&1 | head -5 || true

echo ""
echo "=== Deploy Summary ==="
echo ""
echo "  Watchdog dir:     ${WATCHDOG_DIR}"
echo "  Watchdog cron:    ${CRON_LINE}"
echo "  CC project dir:   ${PROJECT_DIR}"
echo "  CC scripts:       ${CC_SCRIPTS}"
echo "  CC session dir:   ${CC_SESSION_DIR}"
echo ""
echo "Next steps:"
echo "  1. Edit config.env if you haven't: ${CONFIG_ENV}"
echo "  2. Edit settings.json if needed: ${HOME}/.claude/settings.json"
echo "  3. Ensure shell env vars are set in .bashrc/.profile (see ARCHITECTURE.md)"
echo "  4. Start CC session:  bash ${CC_SCRIPTS}/start.sh"
echo "  5. Register CronCreate inside CC session (see templates/cron/register_cron.md)"
echo ""
echo "Monitor:"
echo "  tail -f ${WATCHDOG_LOGS}/watchdog.log"
echo "  screen -r ${SCREEN_NAME}"
echo ""
echo "Uninstall:"
echo "  bash ${SCRIPT_DIR}/deploy.sh --uninstall"