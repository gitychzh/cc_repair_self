#!/bin/bash
# restart_claude.sh — Kill and restart Claude Code in a screen session
#
# 设计要点:
#   - 先 SIGTERM，等 10s，再 SIGKILL（优雅退出优先）
#   - screen session 名称由 SCREEN_NAME 配置
#   - bash --login 确保 .profile → .bashrc 加载（env vars 可用）
#   - 支持 --resume <session_id> 参数恢复上次对话
#   - CC 进程的环境变量来自三层保障:
#     1. .bashrc (在 non-interactive return 之前设置)
#     2. .profile (login shells)
#     3. 本脚本使用 bash --login 启动
#   - 所有输出 tee 到 claude_output.log（watchdog 可读）

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 加载配置（如果在 auto-loop 上下文中）
CONFIG_ENV="${SCRIPT_DIR}/../config.env"
if [ -f "${CONFIG_ENV}" ]; then
  source "${CONFIG_ENV}"
fi

SCREEN_NAME="${SCREEN_NAME:-claude}"
PROJECT_DIR="${PROJECT_DIR:-$(dirname "${SCRIPT_DIR}")}"
CLAUDE_BIN="${CLAUDE_BIN:-${HOME}/.npm-global/bin/claude}"
CC_PERMISSION_MODE="${CC_PERMISSION_MODE:-bypassPermissions}"

echo "=== Restarting Claude Code ==="

# Kill existing Claude Code process (graceful first)
PIDS=$(pgrep -f 'node.*claude' 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "  Found existing Claude processes: $PIDS"
  for pid in $PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  echo "  Sent SIGTERM, waiting 10s..."
  sleep 10
  PIDS=$(pgrep -f 'node.*claude' 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "  Still alive, sending SIGKILL..."
    for pid in $PIDS; do
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 2
  fi
fi

# Kill existing screen session if any
screen -S "${SCREEN_NAME}" -X quit 2>/dev/null || true
sleep 1

# Verify claude binary exists
if [ ! -x "${CLAUDE_BIN}" ]; then
  echo "  ERROR: claude binary not found at ${CLAUDE_BIN}"
  echo "  Searching PATH..."
  CLAUDE_BIN=$(which claude 2>/dev/null || true)
  if [ -z "${CLAUDE_BIN}" ]; then
    echo "  FATAL: Cannot find claude binary anywhere"
    exit 1
  fi
fi
echo "  Using claude at: ${CLAUDE_BIN}"

# Check for --resume argument
RESUME_ARG=""
if [ $# -gt 0 ]; then
  RESUME_ARG="--resume $1"
fi

# Start Claude Code in screen session
# bash --login so .profile is sourced → .bashrc sourced → env vars available
# This ensures ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY etc
# are set before CC's startup connectivity check runs.
screen -dmS "${SCREEN_NAME}" bash --login -c "${CLAUDE_BIN} --permission-mode ${CC_PERMISSION_MODE} ${RESUME_ARG} 2>&1 | tee ${PROJECT_DIR}/claude_output.log"

sleep 3

# Verify it started
if screen -list | grep -q "${SCREEN_NAME}"; then
  echo "  OK: Claude Code screen session started (name: ${SCREEN_NAME})"
else
  echo "  WARN: Screen session not found, Claude Code may have failed to start"
fi

echo "=== Restart complete ==="