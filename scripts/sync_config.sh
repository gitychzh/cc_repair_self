#!/bin/bash
# sync_config.sh — One-click sync from Git repo configs to /opt/cc-infra/
# Usage: bash sync_config.sh [--dry-run]
# Must be run on opc_uname (or opc2_uname) with /opt/cc-infra/ deployed

set -euo pipefail

REPO_DIR="${HOME}/cc_ps/cc_repair_self"
DEPLOY_DIR="/opt/cc-infra"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "=== DRY RUN — no files will be copied ==="
fi

# Files to sync: repo → deploy
SYNC_MAP=(
    "configs/docker-compose.yml:docker-compose.yml"
    "configs/litellm-glm51/config.yaml:litellm-glm51/config.yaml"
    "configs/litellm-dsv4p/config.yaml:litellm-dsv4p/config.yaml"
    "configs/litellm-glm51-test/config.yaml:litellm-glm51-test/config.yaml"
    "configs/proxy/proxy.py:proxy/proxy.py"
    "configs/proxy/Dockerfile:proxy/Dockerfile"
    "configs/postgres/init-db.sh:postgres/init-db.sh"
)

echo "=== Syncing configs from ${REPO_DIR} to ${DEPLOY_DIR} ==="

# Pull latest from GitHub
echo "[1] Pulling latest from GitHub..."
cd "${REPO_DIR}" && git pull

# Backup current configs
BACKUP_TS=$(date +%Y%m%d%H%M%S)
echo "[2] Backing up current configs to ${DEPLOY_DIR}/backups/sync_${BACKUP_TS}/..."
mkdir -p "${DEPLOY_DIR}/backups/sync_${BACKUP_TS}"
for mapping in "${SYNC_MAP[@]}"; do
    src_rel="${mapping%%:*}"
    dst_rel="${mapping##*:}"
    dst="${DEPLOY_DIR}/${dst_rel}"
    if [[ -f "${dst}" ]]; then
        cp "${dst}" "${DEPLOY_DIR}/backups/sync_${BACKUP_TS}/${dst_rel}"
        echo "  backed up: ${dst_rel}"
    fi
done

# Sync files
echo "[3] Syncing files..."
for mapping in "${SYNC_MAP[@]}"; do
    src_rel="${mapping%%:*}"
    dst_rel="${mapping##*:}"
    src="${REPO_DIR}/${src_rel}"
    dst="${DEPLOY_DIR}/${dst_rel}"

    if [[ ! -f "${src}" ]]; then
        echo "  SKIP (source missing): ${src_rel}"
        continue
    fi

    # Check diff
    if diff -q "${src}" "${dst}" &>/dev/null; then
        echo "  SAME: ${dst_rel} (no change needed)"
        continue
    fi

    if $DRY_RUN; then
        echo "  WOULD COPY: ${src_rel} → ${dst_rel}"
        diff "${dst}" "${src}" | head -5
    else
        cp "${src}" "${dst}"
        echo "  COPIED: ${src_rel} → ${dst_rel}"
    fi
done

echo ""
echo "=== Sync complete ==="
echo "Next steps:"
echo "  1. bash scripts/deploy.sh          # Deploy changes (restart affected containers)"
echo "  2. bash scripts/health_check.sh    # Verify all containers healthy"
echo "  3. curl test (see CLAUDE.md)        # Verify 200 response from both models"