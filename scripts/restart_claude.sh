#!/bin/bash
# restart_claude.sh — Kill and restart Claude Code in a screen session
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

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
screen -S claude -X quit 2>/dev/null || true
sleep 1

# Start Claude Code in screen session
# Use full path for claude binary — screen's bash -c runs non-interactive shell,
# which doesn't source .bashrc, so PATH won't include ~/.npm-global/bin.
CLAUDE_BIN="$HOME/.npm-global/bin/claude"
if [ ! -x "$CLAUDE_BIN" ]; then
  echo "  ERROR: claude binary not found at $CLAUDE_BIN"
  echo "  Searching PATH..."
  CLAUDE_BIN=$(which claude 2>/dev/null || true)
  if [ -z "$CLAUDE_BIN" ]; then
    echo "  FATAL: Cannot find claude binary anywhere"
    exit 1
  fi
fi
echo "  Using claude at: $CLAUDE_BIN"

# Check for --resume argument
RESUME_ARG=""
if [ $# -gt 0 ]; then
  RESUME_ARG="--resume $1"
fi

screen -dmS claude bash -c "${CLAUDE_BIN} --permission-mode bypassPermissions ${RESUME_ARG} 2>&1 | tee ${PROJECT_DIR}/claude_output.log"

sleep 3

# Verify it started
if screen -list | grep -q claude; then
  echo "  OK: Claude Code screen session started"
else
  echo "  WARN: Screen session not found, Claude Code may have failed to start"
fi

echo "=== Restart complete ==="