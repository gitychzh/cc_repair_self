#!/bin/bash
# restart_claude.sh — Kill and restart Claude Code in a screen session
# R25: Added 40001 health check with fallback to 40002 proxy
# If 40001 is unavailable (restarting), CC auto-switches to 40002
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Restarting Claude Code ==="

# ─── R25: Proxy health check — auto-detect which proxy to use ───
PROXY_PRIMARY="http://127.0.0.1:40001"
PROXY_FALLBACK="http://127.0.0.1:40002"
PROXY_URL="$PROXY_PRIMARY"

echo "  Checking proxy availability..."
# Quick health check on primary proxy (40001)
PRIMARY_OK=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$PROXY_PRIMARY/health" 2>/dev/null || echo "000")
if [ "$PRIMARY_OK" = "200" ]; then
  echo "  Primary proxy (40001) is UP → using 40001"
  PROXY_URL="$PROXY_PRIMARY"
else
  echo "  Primary proxy (40001) is DOWN (HTTP=$PRIMARY_OK) → checking fallback (40002)"
  FALLBACK_OK=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$PROXY_FALLBACK/health" 2>/dev/null || echo "000")
  if [ "$FALLBACK_OK" = "200" ]; then
    echo "  Fallback proxy (40002) is UP → using 40002"
    PROXY_URL="$PROXY_FALLBACK"
  else
    echo "  WARN: Both proxies DOWN! (40001=$PRIMARY_OK, 40002=$FALLBACK_OK)"
    echo "  Will try primary proxy anyway — CC may fail on startup"
    PROXY_URL="$PROXY_PRIMARY"
  fi
fi

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
# R13 fix: CC v2.1.170+ startup connectivity check requires shell env vars.
# screen's bash -c is non-interactive → doesn't source .bashrc.
# But .bashrc now sets env vars BEFORE non-interactive return, so sourcing
# .bashrc from login shell (.profile) ensures env vars are available.
# For screen sessions, we use bash --login to ensure .profile is sourced.
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

# R25: Set ANTHROPIC_BASE_URL based on health check result
# Override the default in .bashrc — this takes precedence for this session
export ANTHROPIC_BASE_URL="$PROXY_URL"
echo "  ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"

# Use bash --login so .profile is sourced → .bashrc sourced → env vars available
# R25: Pass ANTHROPIC_BASE_URL explicitly so it overrides .bashrc default
screen -dmS claude bash --login -c "export ANTHROPIC_BASE_URL='$PROXY_URL'; ${CLAUDE_BIN} --permission-mode bypassPermissions ${RESUME_ARG} 2>&1 | tee ${PROJECT_DIR}/claude_output.log"

sleep 3

# Verify it started
if screen -list | grep -q claude; then
  echo "  OK: Claude Code screen session started (proxy=$PROXY_URL)"
else
  echo "  WARN: Screen session not found, Claude Code may have failed to start"
fi

echo "=== Restart complete (proxy=$PROXY_URL) ==="
