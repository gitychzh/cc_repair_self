#!/bin/bash
# health_check.sh — Check health of all components, return structured result
# R29: 5 containers (cc_postgres, ms_uni41001, auth_to_api_40001/40002/40003)
set -e

CLAUDE_ALIVE="no"
PROXY_40001_HEALTHY="no"
PROXY_40002_HEALTHY="no"
PROXY_40003_HEALTHY="no"
PROXY_40005_HEALTHY="no"
LITELLM_HEALTHY="no"
CONTAINERS_HEALTHY=0

# Check Claude Code process
if pgrep -f 'claude --permission-mode' > /dev/null 2>&1; then
  CLAUDE_ALIVE="yes"
fi
if pgrep -f 'node.*claude' > /dev/null 2>&1; then
  CLAUDE_ALIVE="yes"
fi

# Check proxy health — 3 proxy containers
if curl -sf http://127.0.0.1:40001/health > /dev/null 2>&1; then
  PROXY_40001_HEALTHY="yes"
fi
if curl -sf http://127.0.0.1:40002/health > /dev/null 2>&1; then
  PROXY_40002_HEALTHY="yes"
fi
if curl -sf http://127.0.0.1:40003/health > /dev/null 2>&1; then
  PROXY_40003_HEALTHY="yes"
fi
# R31: 40005 = PRIMARY CC proxy (opus default). Reported for visibility but NOT in ALL_OK
# (manual failover only; monitor.sh should not auto-recreate it). 40001 is the fallback.
if curl -sf http://127.0.0.1:40005/health > /dev/null 2>&1; then
  PROXY_40005_HEALTHY="yes"
fi

# Check LiteLLM health — MUST use /health/liveliness, NOT /health!
if curl -sf -H "Authorization: Bearer sk-litellm-local" http://127.0.0.1:41001/health/liveliness > /dev/null 2>&1; then
  LITELLM_HEALTHY="yes"
fi

# Check Docker containers (5 containers: cc_postgres, ms_uni41001, auth_to_api_40001/40002/40003)
CONTAINERS_HEALTHY=$(docker ps --filter 'health=healthy' --format '{{.Names}}' | grep -c 'cc_\|ms_\|auth_to' 2>/dev/null || echo "0")

# Show proxy roles
echo "PROXY_40001_ROLE=$(curl -sf http://127.0.0.1:40001/health 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("proxy_role","unknown"))' 2>/dev/null || echo 'unknown')"
echo "PROXY_40002_ROLE=$(curl -sf http://127.0.0.1:40002/health 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("proxy_role","unknown"))' 2>/dev/null || echo 'unknown')"
echo "PROXY_40003_ROLE=$(curl -sf http://127.0.0.1:40003/health 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("proxy_role","unknown"))' 2>/dev/null || echo 'unknown')"
echo "PROXY_40005_ROLE=$(curl -sf http://127.0.0.1:40005/health 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("proxy_role","unknown"))' 2>/dev/null || echo 'down_or_unknown')"

echo "CLAUDE_ALIVE=$CLAUDE_ALIVE"
echo "PROXY_40001_HEALTHY=$PROXY_40001_HEALTHY"
echo "PROXY_40002_HEALTHY=$PROXY_40002_HEALTHY"
echo "PROXY_40003_HEALTHY=$PROXY_40003_HEALTHY"
echo "PROXY_40005_HEALTHY=$PROXY_40005_HEALTHY"
echo "LITELLM_HEALTHY=$LITELLM_HEALTHY"
echo "CONTAINERS_HEALTHY=$CONTAINERS_HEALTHY/6"

ALL_OK="yes"
if [ "$CLAUDE_ALIVE" = "no" ]; then ALL_OK="no"; fi
if [ "$PROXY_40001_HEALTHY" = "no" ]; then ALL_OK="no"; fi
if [ "$PROXY_40003_HEALTHY" = "no" ]; then ALL_OK="no"; fi
if [ "$LITELLM_HEALTHY" = "no" ]; then ALL_OK="no"; fi

echo "ALL_OK=$ALL_OK"
