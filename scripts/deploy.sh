#!/bin/bash
# deploy.sh — One-click deploy + restart + test for cc-infra
# Usage: bash deploy.sh [service]
#   No args: full redeploy (all services)
#   service: only restart specified container (e.g. ms_uni41001)
# Must be run on opc_uname (or opc2_uname) with /opt/cc-infra/ deployed
# R29: Three-proxy (40001=cc, 40002=codex, 40003=passthrough) + dsv4p backend + no fallback

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
    echo "[1] Rebuilding 40001 proxy container (PROXY_ROLE=cc)..."
    DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001
elif [[ "${SERVICE}" == "proxy40002" ]] || [[ "${SERVICE}" == "auth_to_api_40002" ]]; then
    echo "[1] Rebuilding 40002 proxy container (PROXY_ROLE=codex)..."
    DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40002
elif [[ "${SERVICE}" == "proxy40003" ]] || [[ "${SERVICE}" == "auth_to_api_40003" ]]; then
    echo "[1] Rebuilding 40003 proxy container (PROXY_ROLE=passthrough)..."
    DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40003
elif [[ "${SERVICE}" == "proxy-all" ]]; then
    echo "[1] Rebuilding all 3 proxy containers (40001 + 40002 + 40003)..."
    DOCKER_BUILDKIT=0 docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002 auth_to_api_40003
elif [[ "${SERVICE}" == "ms_uni41001" ]]; then
    echo "[1] Restarting ms_uni41001 (LiteLLM config changed)..."
    docker restart ms_uni41001
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

# Test glm5.2 via proxy 40001 (Anthropic format, CC)
echo "  Testing glm5.2 via 40001 (CC proxy)..."
GLM_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X POST http://127.0.0.1:40001/v1/messages \
    -H "Content-Type: application/json" \
    -H "x-api-key: sk-litellm-local" \
    -H "anthropic-version: 2023-06-01" \
    -d '{"model":"glm5.2","messages":[{"role":"user","content":"test"}],"max_tokens":50}')
echo "  glm5.2 via 40001 HTTP status: ${GLM_RESULT}"

# Test glm5.2_cx via proxy 40002 (Codex, Responses API)
echo "  Testing glm5.2_cx via 40002 (Codex proxy)..."
CX_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X POST http://127.0.0.1:40002/v1/responses \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer sk-litellm-local" \
    -d '{"model":"glm5.2_cx","input":"test"}')
echo "  glm5.2_cx via 40002 HTTP status: ${CX_RESULT}"

# Test dsv4p via proxy 40003 (OpenAI passthrough, OpenClaw/OpenCode/Hermes)
echo "  Testing dsv4p via 40003 (Passthrough proxy)..."
DSV4P_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X POST http://127.0.0.1:40003/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer sk-litellm-local" \
    -d '{"model":"dsv4p_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}')
echo "  dsv4p via 40003 HTTP status: ${DSV4P_RESULT}"

# Test LiteLLM 41001 health (direct)
echo "  Testing LiteLLM 41001 health..."
LITELLM_41001=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:41001/health/liveliness)
echo "  LiteLLM 41001 health: ${LITELLM_41001}"

# Test role isolation: 40001 should reject /v1/chat/completions
echo "  Testing role isolation (40001 should reject /v1/chat/completions)..."
ISOLATION_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST http://127.0.0.1:40001/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer sk-litellm-local" \
    -d '{"model":"test","messages":[{"role":"user","content":"test"}],"max_tokens":50}')
echo "  40001 /v1/chat/completions HTTP status: ${ISOLATION_RESULT} (expected: 404)"

echo ""
if [[ "${GLM_RESULT}" == "200" ]]; then
    echo "=== CC proxy (40001) OK — glm5.2 working ==="
else
    echo "=== WARNING — glm5.2 via 40001 returned ${GLM_RESULT}, check logs ==="
fi
if [[ "${CX_RESULT}" == "200" ]]; then
    echo "=== Codex proxy (40002) OK — glm5.2_cx working ==="
else
    echo "=== WARNING — glm5.2_cx via 40002 returned ${CX_RESULT}, check logs ==="
fi
if [[ "${DSV4P_RESULT}" == "200" ]]; then
    echo "=== Passthrough proxy (40003) OK — dsv4p working ==="
else
    echo "=== WARNING — dsv4p via 40003 returned ${DSV4P_RESULT}, check logs ==="
fi
if [[ "${ISOLATION_RESULT}" == "404" ]]; then
    echo "=== Role isolation OK — 40001 correctly rejects /v1/chat/completions ==="
else
    echo "=== WARNING — Role isolation may not be working (expected 404, got ${ISOLATION_RESULT}) ==="
fi
