#!/bin/bash
# sync_config.sh — One-click sync from Git repo configs to /opt/cc-infra/
# Usage: bash sync_config.sh [--dry-run]
# Must be run on opc_uname (or opc2_uname) with /opt/cc-infra/ deployed
# R29: Updated for gateway module package + gateway_main.py entry point + no proxy.py

set -euo pipefail

REPO_DIR="${HOME}/cc_ps/cc_repair_self"
DEPLOY_DIR="/opt/cc-infra"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "=== DRY RUN — no files will be copied ==="
fi

# Files to sync: repo → deploy
# R29: gateway module package (all .py files) + gateway_main.py + Dockerfile
SYNC_MAP=(
    "configs/docker-compose.yml:docker-compose.yml"
    "configs/litellm-glm51/config.yaml:litellm-glm51/config.yaml"
    "configs/proxy/gateway_main.py:proxy/gateway_main.py"
    "configs/proxy/Dockerfile:proxy/Dockerfile"
    "configs/proxy/gateway/__init__.py:proxy/gateway/__init__.py"
    "configs/proxy/gateway/app.py:proxy/gateway/app.py"
    "configs/proxy/gateway/config.py:proxy/gateway/config.py"
    "configs/proxy/gateway/handlers.py:proxy/gateway/handlers.py"
    "configs/proxy/gateway/upstream.py:proxy/gateway/upstream.py"
    "configs/proxy/gateway/converters.py:proxy/gateway/converters.py"
    "configs/proxy/gateway/stream.py:proxy/gateway/stream.py"
    "configs/proxy/gateway/error_mapping.py:proxy/gateway/error_mapping.py"
    "configs/proxy/gateway/codex.py:proxy/gateway/codex.py"
    "configs/proxy/gateway/logger.py:proxy/gateway/logger.py"
    "configs/postgres/init-db.sh:postgres/init-db.sh"
)

echo "=== Syncing configs from ${REPO_DIR} to ${DEPLOY_DIR} ==="

# Pull latest from GitHub
echo "[1] Pulling latest from GitHub..."
cd "${REPO_DIR}" && git pull

# Backup current configs
BACKUP_TS=$(date +%Y%m%d%H%M%S)
echo "[2] Backing up current configs to ${DEPLOY_DIR}/backups/sync_${BACKUP_TS}/..."
for mapping in "${SYNC_MAP[@]}"; do
    dst_rel="${mapping##*:}"
    dst="${DEPLOY_DIR}/${dst_rel}"
    if [[ -f "${dst}" ]]; then
        backup_dir="${DEPLOY_DIR}/backups/sync_${BACKUP_TS}"
        # Create parent directories for the backup file
        mkdir -p "$(dirname "${backup_dir}/${dst_rel}")"
        cp "${dst}" "${backup_dir}/${dst_rel}"
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

    # Ensure destination directory exists
    mkdir -p "$(dirname "${dst}")"

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

# Sync .env template (reference only, not directly used by docker-compose)
echo "[4] Checking .env template..."
if [[ -f "${DEPLOY_DIR}/.env" ]]; then
    echo "  .env exists (not overwritten — manual update required for new env vars)"
    # Check if new R29 env vars are present
    if ! grep -q "NUM_VARIANTS_DSV4P" "${DEPLOY_DIR}/.env" 2>/dev/null; then
        echo "  ⚠️  .env missing NUM_VARIANTS_DSV4P — add it for R29"
    fi
    if ! grep -q "MODEL_INPUT_TOKEN_SAFETY_DSV4P" "${DEPLOY_DIR}/.env" 2>/dev/null; then
        echo "  ⚠️  .env missing MODEL_INPUT_TOKEN_SAFETY_DSV4P — add it for R29"
    fi
    if ! grep -q "LITELLM_URL_DSV4P" "${DEPLOY_DIR}/.env" 2>/dev/null; then
        echo "  ⚠️  .env missing LITELLM_URL_DSV4P — add it for R29"
    fi
fi

echo ""
echo "=== Sync complete ==="
echo "Next steps:"
echo "  1. Update .env with new R29 env vars (NUM_VARIANTS_DSV4P, MODEL_INPUT_TOKEN_SAFETY_DSV4P, etc.)"
echo "  2. bash scripts/deploy.sh ms_uni41001    # Restart LiteLLM with 140 dep config"
echo "  3. docker stop ms_uni41002 && docker rm ms_uni41002  # Remove old fallback container"
echo "  4. bash scripts/deploy.sh proxy-all      # Rebuild all 3 proxy containers"
echo "  5. bash scripts/deploy.sh all            # Full rebuild (5 containers)"
echo "  6. bash scripts/health_check.sh          # Verify all containers healthy"
echo "  7. curl test (see CLAUDE.md)             # Verify glm5.1 + dsv4p return 200"
