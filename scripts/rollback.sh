#!/bin/bash
# rollback.sh <backup_dir> — Restore configs from backup and restart affected services
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_BASE="${PROJECT_DIR}/backups"

BACKUP_DIR="$1"
if [ -z "$BACKUP_DIR" ]; then
  echo "Usage: rollback.sh <backup_dir>"
  echo "Available backups:"
  ls -1 "$BACKUP_BASE" | sort -r | head -5
  exit 1
fi

# Accept relative path from backup base
if [ ! -d "$BACKUP_DIR" ]; then
  BACKUP_DIR="${BACKUP_BASE}/${BACKUP_DIR}"
fi

if [ ! -d "$BACKUP_DIR" ]; then
  echo "ERROR: Backup directory not found: $BACKUP_DIR"
  exit 1
fi

echo "=== Rolling back from $BACKUP_DIR ==="

HOME_DIR="$HOME"

# Restore settings.json
if [ -f "$BACKUP_DIR/claude_settings.json" ]; then
  cp "$BACKUP_DIR/claude_settings.json" "${HOME_DIR}/.claude/settings.json"
  echo "  Restored: settings.json"
fi

# Restore proxy.py
if [ -f "$BACKUP_DIR/proxy/proxy.py" ]; then
  cp "$BACKUP_DIR/proxy/proxy.py" /opt/cc-infra/proxy/proxy.py
  echo "  Restored: proxy.py"
fi

# Restore litellm-glm51 config
if [ -f "$BACKUP_DIR/litellm-glm51/config.yaml" ]; then
  cp "$BACKUP_DIR/litellm-glm51/config.yaml" /opt/cc-infra/litellm-glm51/config.yaml
  echo "  Restored: litellm-glm51/config.yaml"
fi

# Restore litellm-dsv4p config
if [ -f "$BACKUP_DIR/litellm-dsv4p/config.yaml" ]; then
  cp "$BACKUP_DIR/litellm-dsv4p/config.yaml" /opt/cc-infra/litellm-dsv4p/config.yaml
  echo "  Restored: litellm-dsv4p/config.yaml"
fi

# Restore docker-compose.yml
if [ -f "$BACKUP_DIR/docker-compose.yml" ]; then
  cp "$BACKUP_DIR/docker-compose.yml" /opt/cc-infra/docker-compose.yml
  echo "  Restored: docker-compose.yml"
fi

# Restore .env
if [ -f "$BACKUP_DIR/.env" ]; then
  cp "$BACKUP_DIR/.env" /opt/cc-infra/.env
  echo "  Restored: .env"
fi

# Restart affected services
cd /opt/cc-infra

if [ -f "$BACKUP_DIR/proxy/proxy.py" ]; then
  echo "  Rebuilding proxy container..."
  docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002
  sleep 5
fi

if [ -f "$BACKUP_DIR/litellm-glm51/config.yaml" ]; then
  echo "  Restarting glm5.1 LiteLLM..."
  docker restart glm5.1_uni41001
  sleep 10
fi

if [ -f "$BACKUP_DIR/litellm-dsv4p/config.yaml" ]; then
  echo "  Restarting dsv4p LiteLLM..."
  docker restart dsv4p_uni42001
  sleep 10
fi

if [ -f "$BACKUP_DIR/docker-compose.yml" ]; then
  echo "  Recreating all services from compose..."
  docker compose up -d --force-recreate
  sleep 15
fi

# Restart Claude Code if needed
bash "${SCRIPT_DIR}/restart_claude.sh"

echo "=== Rollback complete ==="
echo "=== Running health check ==="
bash "${SCRIPT_DIR}/health_check.sh"