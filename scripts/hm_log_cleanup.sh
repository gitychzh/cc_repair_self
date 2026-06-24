#!/bin/bash
# R40: Cleanup hm_requests / hm_tier_attempts older than N days (default 30).
# Runs the hm_cleanup_old() SQL function. Safe to run via cron daily.
# Usage: bash scripts/hm_log_cleanup.sh [days]

set -euo pipefail
DAYS="${1:-30}"
docker exec -i cc_postgres psql -U litellm -d hermes_logs -P pager=off \
  -c "SELECT hm_cleanup_old($DAYS) AS rows_deleted;"
