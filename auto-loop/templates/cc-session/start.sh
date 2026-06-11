#!/bin/bash
# start.sh — 一键启动 CC 的完整自循环 session
#
# 这个脚本做了三件事:
#   1. source config.env → 加载所有配置变量
#   2. 启动 CC (在 screen 中)
#   3. 向 CC 发送初始 prompt 注册 CronCreate 定时任务
#
# 使用方法:
#   bash start.sh                     # 首次启动（新 session）
#   bash start.sh <session_id>        # 恢复上次 session (--resume)
#
# 注意: CronCreate 定时任务需要在 CC 对话内部注册。
#       本脚本启动 CC 后，你需要手动进入 screen session，
#       等待 CC 就绪，然后发送 CronCreate prompt（见 cron/register_cron.md）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 加载配置
CONFIG_ENV="${SCRIPT_DIR}/../config.env"
if [ ! -f "${CONFIG_ENV}" ]; then
  echo "ERROR: config.env not found at ${CONFIG_ENV}"
  echo "Please copy config.env.template to config.env and fill in your values."
  exit 1
fi
source "${CONFIG_ENV}"

echo "=== Starting Auto-Loop Session ==="
echo "  Project:    ${PROJECT_DIR}"
echo "  Model:      ${CC_MODEL}"
echo "  Screen:     ${SCREEN_NAME}"

# 1. 确保 env vars 在 shell 层面可用
#    CC v2.1.170+ 的 startup connectivity check 用 shell env vars，不读 settings.json
#    我们需要在 .bashrc 和 .profile 中设置这些变量
echo ""
echo "[1] Checking shell env vars..."

ENV_VARS_TO_CHECK=(
  "ANTHROPIC_BASE_URL"
  "ANTHROPIC_API_KEY"
)

MISSING_VARS=()
for var in "${ENV_VARS_TO_CHECK[@]}"; do
  if [ -z "$(bash --login -c "echo \$${var}" 2>/dev/null)" ]; then
    MISSING_VARS+=("${var}")
  fi
done

if [ "${#MISSING_VARS[@]}" -gt 0 ]; then
  echo "  WARNING: Missing shell env vars: ${MISSING_VARS[*]}"
  echo "  CC startup may fail. Set these in .bashrc/.profile before proceeding."
  echo "  See ARCHITECTURE.md 'CC v2.1.170 startup' section."
fi

# 2. 启动 CC
echo ""
echo "[2] Starting Claude Code..."

RESUME_ARG="${1:-}"
if [ -n "${RESUME_ARG}" ]; then
  bash "${SCRIPT_DIR}/restart_claude.sh" "${RESUME_ARG}"
else
  bash "${SCRIPT_DIR}/restart_claude.sh"
fi

# 3. 提示注册 CronCreate
echo ""
echo "[3] Register CronCreate inside CC session:"
echo "  Enter the screen session:  screen -r ${SCREEN_NAME}"
echo "  Wait for CC to be ready, then follow instructions in:"
echo "    auto-loop/templates/cron/register_cron.md"
echo ""
echo "=== Auto-Loop Session started ==="