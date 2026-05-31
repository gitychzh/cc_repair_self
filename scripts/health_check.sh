#!/bin/bash
# health_check.sh — Check health of all components, return structured result
set -e

CLAUDE_ALIVE="no"
PROXY_HEALTHY="no"
LITELLM_GLM51_HEALTHY="no"
LITELLM_DSV4P_HEALTHY="no"
CONTAINERS_HEALTHY=0

# Check Claude Code process
if pgrep -f 'claude --permission-mode' > /dev/null 2>&1; then
  CLAUDE_ALIVE="yes"
fi
if pgrep -f 'node.*claude' > /dev/null 2>&1; then
  CLAUDE_ALIVE="yes"
fi

# Check proxy health
if curl -sf http://127.0.0.1:40001/health > /dev/null 2>&1; then
  PROXY_HEALTHY="yes"
fi

# Check LiteLLM GLM-5.1 health
if curl -sf -H "Authorization: Bearer sk-litellm-local" http://127.0.0.1:41001/health > /dev/null 2>&1; then
  LITELLM_GLM51_HEALTHY="yes"
fi

# Check LiteLLM DSv4P health
if curl -sf -H "Authorization: Bearer sk-litellm-local" http://127.0.0.1:42001/health > /dev/null 2>&1; then
  LITELLM_DSV4P_HEALTHY="yes"
fi

# Check Docker containers
CONTAINERS_HEALTHY=$(docker ps --filter 'health=healthy' --format '{{.Names}}' | wc -l)
TOTAL_CONTAINERS=$(docker ps --format '{{.Names}}' | grep -c 'cc_\|glm5\|dsv4p\|auth_to' 2>/dev/null || echo "0")

echo "CLAUDE_ALIVE=$CLAUDE_ALIVE"
echo "PROXY_HEALTHY=$PROXY_HEALTHY"
echo "LITELLM_GLM51_HEALTHY=$LITELLM_GLM51_HEALTHY"
echo "LITELLM_DSV4P_HEALTHY=$LITELLM_DSV4P_HEALTHY"
echo "CONTAINERS_HEALTHY=$CONTAINERS_HEALTHY/4"

ALL_OK="yes"
if [ "$CLAUDE_ALIVE" = "no" ]; then ALL_OK="no"; fi
if [ "$PROXY_HEALTHY" = "no" ]; then ALL_OK="no"; fi
if [ "$LITELLM_GLM51_HEALTHY" = "no" ]; then ALL_OK="no"; fi

echo "ALL_OK=$ALL_OK"