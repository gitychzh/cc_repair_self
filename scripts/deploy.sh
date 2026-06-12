#!/bin/bash
# deploy.sh — One-click deploy + restart + test for cc-infra
# Usage: bash deploy.sh [service]
#   No args: full redeploy (all services)
#   service: only restart specified container (e.g. ms_uni41001)
# Must be run on opc_uname (or opc2_uname) with /opt/cc-infra/ deployed
# R26: Added ms_uni41002 LiteLLM fallback + deploy options

set -euo pipefail

DEPLOY_DIR="/opt/cc-infra"
SERVICE="${1:-all}"

cd "${DEPLOY_DIR}"

echo "=== Deploying cc-infra (service: ${SERVICE}) ==="

# Detect what changed and restart accordingly
if [[ "${SERVICE}" == "all" ]]; then
    echo "[1] Full redeploy — recreating all containers..."
    DOCKER_BUILDKIT=0 docker compose up -d --force-recreate
elif [[ "${SERVICE}" == "proxy" ]] || [[ "${SERVICE}" == "auth_to_api_40001" ]]; then
    echo "[1] Rebuilding 40001 proxy container (proxy.py changed)..."
    DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001
elif [[ "${SERVICE}" == "proxy40002" ]] || [[ "${SERVICE}" == "auth_to_api_40002" ]]; then
    echo "[1] Rebuilding 40002 proxy container..."
    DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40002
elif [[ "${SERVICE}" == "proxy-all" ]]; then
    echo "[1] Rebuilding both proxy containers (40001 + 40002)..."
    DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002
elif [[ "${SERVICE}" == "ms_uni41001" ]] || [[ "${SERVICE}" == "glm5.1_uni41001" ]]; then
    echo "[1] Restarting ms_uni41001 (LiteLLM config changed)..."
    docker restart ms_uni41001
elif [[ "${SERVICE}" == "ms_uni41002" ]] || [[ "${SERVICE}" == "litellm-fallback" ]]; then
    echo "[1] Restarting ms_uni41002 (fallback LiteLLM)..."
    docker restart ms_uni41002
elif [[ "${SERVICE}" == "litellm-all" ]]; then
    echo "[1] Restarting both LiteLLM containers (41001 + 41002)..."
    docker restart ms_uni41001 ms_uni41002
else
    echo "[1] Restarting ${SERVICE}..."
    docker restart "${SERVICE}"
fi

echo ""
echo "[2] Waiting for containers to stabilize (10s)..."
sleep 10

echo ""
echo "[3] Checking container status..."
docker ps --format 'table {{.Names}}\t{{.Status}}' | head -10

echo ""
echo "[4] Testing requests..."

# Test glm5.1 via proxy 40001 (Anthropic format)
echo "  Testing glm5.1 via 40001..."
GLM_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X POST http://127.0.0.1:40001/v1/messages \
    -H "Content-Type: application/json" \
    -H "x-api-key: sk-litellm-local" \
    -H "anthropic-version: 2023-06-01" \
    -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50}')
echo "  glm5.1 via 40001 HTTP status: ${GLM_RESULT}"

# Test glm5.1 via proxy 40002 (Anthropic format) — R25 fallback
echo "  Testing glm5.1 via 40002 (fallback)..."
GLM_RESULT_40002=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X POST http://127.0.0.1:40002/v1/messages \
    -H "Content-Type: application/json" \
    -H "x-api-key: sk-litellm-local" \
    -H "anthropic-version: 2023-06-01" \
    -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50}')
echo "  glm5.1 via 40002 HTTP status: ${GLM_RESULT_40002}"

# Test LiteLLM 41001 health (direct)
echo "  Testing LiteLLM 41001 health..."
LITELLM_41001=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:41001/health/liveliness)
echo "  LiteLLM 41001 health: ${LITELLM_41001}"

# Test LiteLLM 41002 health (direct, R26 fallback)
echo "  Testing LiteLLM 41002 health..."
LITELLM_41002=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:41002/health/liveliness)
echo "  LiteLLM 41002 health: ${LITELLM_41002}"

echo ""
if [[ "${GLM_RESULT}" == "200" ]]; then
    echo "=== Deploy SUCCESS — glm5.1 working via 40001 proxy ==="
else
    echo "=== WARNING — glm5.1 via 40001 returned ${GLM_RESULT}, check logs ==="
fi
if [[ "${GLM_RESULT_40002}" == "200" ]]; then
    echo "=== Fallback 40002 also working ==="
else
    echo "=== WARNING — glm5.1 via 40002 returned ${GLM_RESULT_40002}, check logs ==="
fi
