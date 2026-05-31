#!/bin/bash
# backup_config.sh — Backup all critical configs with timestamp and hashes
set -e

# Resolve project root relative to this script
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_BASE="${PROJECT_DIR}/backups"

TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_DIR="${BACKUP_BASE}/${TIMESTAMP}"
mkdir -p "$BACKUP_DIR"

HOME_DIR="$HOME"

CONFIG_FILES=(
  "${HOME_DIR}/.claude/settings.json"
  "/opt/cc-infra/proxy/proxy.py"
  "/opt/cc-infra/litellm-glm51/config.yaml"
  "/opt/cc-infra/litellm-dsv4p/config.yaml"
  "/opt/cc-infra/docker-compose.yml"
  "/opt/cc-infra/.env"
)

echo "=== Backup to $BACKUP_DIR ==="

for f in "${CONFIG_FILES[@]}"; do
  if [ -f "$f" ]; then
    RELATIVE=$(echo "$f" | sed "s|^/opt/cc-infra/||; s|^${HOME_DIR}/.claude/|claude_|")
    DEST="$BACKUP_DIR/$RELATIVE"
    mkdir -p "$(dirname "$DEST")"
    cp "$f" "$DEST"
    HASH=$(sha256sum "$f" | cut -d' ' -f1)
    echo "  OK: $f -> $RELATIVE (sha256: $HASH)"
  else
    echo "  SKIP: $f (not found)"
  fi
done

# Record current process and container state
echo "--- Process State ---" > "$BACKUP_DIR/state.txt"
ps aux | grep '[c]laude' >> "$BACKUP_DIR/state.txt" 2>/dev/null || true
echo "--- Docker State ---" >> "$BACKUP_DIR/state.txt"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" >> "$BACKUP_DIR/state.txt" 2>/dev/null || true

echo "BACKUP_DIR=$BACKUP_DIR"