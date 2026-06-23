#!/bin/bash
# sync_config.sh — One-click sync from Git repo configs to /opt/cc-infra/
# Usage: bash sync_config.sh [--dry-run]
# Must be run on opc_uname (or opc2_uname) with /opt/cc-infra/ deployed
# R32: Updated for physical proxy split (cc-proxy/codex-proxy/passthrough-proxy/dispatcher)

set -euo pipefail

REPO_DIR="${HOME}/cc_ps/cc_repair_self"
DEPLOY_DIR="/opt/cc-infra"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "=== DRY RUN — no files will be copied ==="
fi

# Files to sync: repo → deploy
# R32: All proxy directories physically isolated
SYNC_MAP=(
    # Docker compose & LiteLLM
    "configs/docker-compose.yml:docker-compose.yml"
    "configs/litellm-glm51/config.yaml:litellm-glm51/config.yaml"

    # Dispatcher (40000)
    "configs/proxy/dispatcher/Dockerfile:proxy/dispatcher/Dockerfile"
    "configs/proxy/dispatcher/gateway_main.py:proxy/dispatcher/gateway_main.py"
    "configs/proxy/dispatcher/gateway/__init__.py:proxy/dispatcher/gateway/__init__.py"
    "configs/proxy/dispatcher/gateway/gateway_main.py:proxy/dispatcher/gateway/gateway_main.py"

    # CC-proxy (40005 primary, 40001 fallback builds from ./proxy for now)
    "configs/proxy/cc-proxy/Dockerfile:proxy/cc-proxy/Dockerfile"
    "configs/proxy/cc-proxy/gateway_main.py:proxy/cc-proxy/gateway_main.py"
    "configs/proxy/cc-proxy/gateway/__init__.py:proxy/cc-proxy/gateway/__init__.py"
    "configs/proxy/cc-proxy/gateway/app.py:proxy/cc-proxy/gateway/app.py"
    "configs/proxy/cc-proxy/gateway/config.py:proxy/cc-proxy/gateway/config.py"
    "configs/proxy/cc-proxy/gateway/handlers.py:proxy/cc-proxy/gateway/handlers.py"
    "configs/proxy/cc-proxy/gateway/upstream.py:proxy/cc-proxy/gateway/upstream.py"
    "configs/proxy/cc-proxy/gateway/converters.py:proxy/cc-proxy/gateway/converters.py"
    "configs/proxy/cc-proxy/gateway/stream.py:proxy/cc-proxy/gateway/stream.py"
    "configs/proxy/cc-proxy/gateway/error_mapping.py:proxy/cc-proxy/gateway/error_mapping.py"
    "configs/proxy/cc-proxy/gateway/logger.py:proxy/cc-proxy/gateway/logger.py"

    # Codex-proxy (40002)
    "configs/proxy/codex-proxy/Dockerfile:proxy/codex-proxy/Dockerfile"
    "configs/proxy/codex-proxy/gateway_main.py:proxy/codex-proxy/gateway_main.py"
    "configs/proxy/codex-proxy/gateway/__init__.py:proxy/codex-proxy/gateway/__init__.py"
    "configs/proxy/codex-proxy/gateway/app.py:proxy/codex-proxy/gateway/app.py"
    "configs/proxy/codex-proxy/gateway/config.py:proxy/codex-proxy/gateway/config.py"
    "configs/proxy/codex-proxy/gateway/handlers.py:proxy/codex-proxy/gateway/handlers.py"
    "configs/proxy/codex-proxy/gateway/upstream.py:proxy/codex-proxy/gateway/upstream.py"
    "configs/proxy/codex-proxy/gateway/converters.py:proxy/codex-proxy/gateway/converters.py"
    "configs/proxy/codex-proxy/gateway/stream.py:proxy/codex-proxy/gateway/stream.py"
    "configs/proxy/codex-proxy/gateway/error_mapping.py:proxy/codex-proxy/gateway/error_mapping.py"
    "configs/proxy/codex-proxy/gateway/logger.py:proxy/codex-proxy/gateway/logger.py"
    "configs/proxy/codex-proxy/gateway/codex.py:proxy/codex-proxy/gateway/codex.py"

    # Passthrough-proxy (40003)
    "configs/proxy/passthrough-proxy/Dockerfile:proxy/passthrough-proxy/Dockerfile"
    "configs/proxy/passthrough-proxy/gateway_main.py:proxy/passthrough-proxy/gateway_main.py"
    "configs/proxy/passthrough-proxy/gateway/__init__.py:proxy/passthrough-proxy/gateway/__init__.py"
    "configs/proxy/passthrough-proxy/gateway/app.py:proxy/passthrough-proxy/gateway/app.py"
    "configs/proxy/passthrough-proxy/gateway/config.py:proxy/passthrough-proxy/gateway/config.py"
    "configs/proxy/passthrough-proxy/gateway/handlers.py:proxy/passthrough-proxy/gateway/handlers.py"
    "configs/proxy/passthrough-proxy/gateway/upstream.py:proxy/passthrough-proxy/gateway/upstream.py"
    "configs/proxy/passthrough-proxy/gateway/converters.py:proxy/passthrough-proxy/gateway/converters.py"
    "configs/proxy/passthrough-proxy/gateway/stream.py:proxy/passthrough-proxy/gateway/stream.py"
    "configs/proxy/passthrough-proxy/gateway/error_mapping.py:proxy/passthrough-proxy/gateway/error_mapping.py"
    "configs/proxy/passthrough-proxy/gateway/logger.py:proxy/passthrough-proxy/gateway/logger.py"
    "configs/proxy/passthrough-proxy/gateway/codex.py:proxy/passthrough-proxy/gateway/codex.py"

    # NV LiteLLM containers (R36: 41101-41105, 1 key each)
    "configs/litellm-nv/config-k1.yaml:litellm-nv/config-k1.yaml"
    "configs/litellm-nv/config-k2.yaml:litellm-nv/config-k2.yaml"
    "configs/litellm-nv/config-k3.yaml:litellm-nv/config-k3.yaml"
    "configs/litellm-nv/config-k4.yaml:litellm-nv/config-k4.yaml"
    "configs/litellm-nv/config-k5.yaml:litellm-nv/config-k5.yaml"

    # R37: NV HM LiteLLM containers (41101-41105, 4 dep each: kimi/glm/minimax/deepseek)
    "configs/litellm-nv-hm/config-k1.yaml:litellm-nv-hm/config-k1.yaml"
    "configs/litellm-nv-hm/config-k2.yaml:litellm-nv-hm/config-k2.yaml"
    "configs/litellm-nv-hm/config-k3.yaml:litellm-nv-hm/config-k3.yaml"
    "configs/litellm-nv-hm/config-k4.yaml:litellm-nv-hm/config-k4.yaml"
    "configs/litellm-nv-hm/config-k5.yaml:litellm-nv-hm/config-k5.yaml"

    # R37: Hermes专用 NV proxy (hm40006)
    "configs/proxy/hm-proxy/Dockerfile:proxy/hm-proxy/Dockerfile"
    "configs/proxy/hm-proxy/gateway_main.py:proxy/hm-proxy/gateway_main.py"
    "configs/proxy/hm-proxy/gateway/__init__.py:proxy/hm-proxy/gateway/__init__.py"
    "configs/proxy/hm-proxy/gateway/app.py:proxy/hm-proxy/gateway/app.py"
    "configs/proxy/hm-proxy/gateway/config.py:proxy/hm-proxy/gateway/config.py"
    "configs/proxy/hm-proxy/gateway/handlers.py:proxy/hm-proxy/gateway/handlers.py"
    "configs/proxy/hm-proxy/gateway/upstream.py:proxy/hm-proxy/gateway/upstream.py"
    "configs/proxy/hm-proxy/gateway/error_mapping.py:proxy/hm-proxy/gateway/error_mapping.py"
    "configs/proxy/hm-proxy/gateway/logger.py:proxy/hm-proxy/gateway/logger.py"
    "configs/proxy/hm-proxy/.gitignore:proxy/hm-proxy/.gitignore"

    # Mihomo proxy config (R36: per-key NV proxy groups)
    "configs/mihomo/config-opc_uname.yaml:mihomo/config-opc_uname.yaml"
    "configs/mihomo/config-opc2_uname.yaml:mihomo/config-opc2_uname.yaml"

    # Postgres
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
    # Check if MIN_OUTBOUND_INTERVAL_S is present (R31.9 throttle)
    if ! grep -q "MIN_OUTBOUND_INTERVAL_S" "${DEPLOY_DIR}/.env" 2>/dev/null; then
        echo "  ⚠️  .env missing MIN_OUTBOUND_INTERVAL_S — docker-compose.yml provides default 2.0"
    fi
fi

echo ""
echo "=== Sync complete ==="
echo "Next steps:"
echo "  1. cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40000 auth_to_api_40001 auth_to_api_40005 auth_to_api_40002 auth_to_api_40003"
echo "  2. docker restart ms_uni41001  # LiteLLM config (volume-mounted)"
echo "  3. curl test (see CLAUDE.md)  # Verify all endpoints return 200"
