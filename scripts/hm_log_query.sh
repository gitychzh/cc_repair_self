#!/bin/bash
# R40: Query helpers for hm40006 logs in cc_postgres hermes_logs DB.
# Usage: bash scripts/hm_log_query.sh <command> [args]
# Commands:
#   recent-fails [N]      — last N failed requests (default 20)
#   tier-health           — per-tier success rate last 1h (view)
#   key-errors            — per-key error distribution last 24h (view)
#   fallback-stats        — fallback occurrence + success rate last 24h
#   single-tier-fails     — failures where only 1 tier was tried (R40 root-cause detector)
#   request <request_id>  — full detail for a request + its tier attempts
#   count-by-status [h]   — status code histogram last h hours (default 24)
#   tail [N]              — last N requests

set -euo pipefail

DB_USER="${HM_DB_USER:-litellm}"
DB_NAME="${HM_DB_NAME:-hermes_logs}"
DB_HOST="${HM_DB_HOST:-cc_postgres}"
PSQL="docker exec -i cc_postgres psql -U $DB_USER -d $DB_NAME -P pager=off"

cmd="${1:-recent-fails}"
case "$cmd" in
  recent-fails)
    N="${2:-20}"
    $PSQL -c "SELECT request_id, ts, host_machine, mapped_model, tier_model, status,
                     duration_ms, tiers_tried_count, fallback_actually_attempted, error_type
              FROM hm_requests
              WHERE status >= 400
              ORDER BY ts DESC LIMIT $N;"
    ;;
  tier-health)
    $PSQL -c "SELECT * FROM v_hm_tier_health_1h;"
    ;;
  key-errors)
    $PSQL -c "SELECT * FROM v_hm_key_errors_24h;"
    ;;
  fallback-stats)
    $PSQL -c "SELECT host_machine,
                     COUNT(*) FILTER (WHERE fallback_occurred) AS fallbacks,
                     COUNT(*) FILTER (WHERE fallback_occurred AND status=200) AS fallback_ok,
                     COUNT(*) AS total,
                     ROUND(100.0 * COUNT(*) FILTER (WHERE fallback_occurred AND status=200)
                           / NULLIF(COUNT(*) FILTER (WHERE fallback_occurred),0), 1) AS fb_success_pct
              FROM hm_requests
              WHERE ts > NOW() - INTERVAL '24 hours'
              GROUP BY host_machine;"
    ;;
  single-tier-fails)
    # R40 root-cause detector: failures where fallback was NOT actually attempted
    $PSQL -c "SELECT request_id, ts, host_machine, mapped_model, tier_model,
                     duration_ms, error_type, tiers_tried_count, fallback_actually_attempted
              FROM hm_requests
              WHERE status >= 400 AND NOT fallback_actually_attempted
              ORDER BY ts DESC LIMIT 50;"
    ;;
  request)
    rid="${2:?need request_id}"
    $PSQL -c "SELECT * FROM hm_requests WHERE request_id='$rid';"
    $PSQL -c "SELECT id, tier, nv_key_idx, error_type, elapsed_ms, ts
              FROM hm_tier_attempts WHERE request_id='$rid' ORDER BY id;"
    ;;
  count-by-status)
    H="${2:-24}"
    $PSQL -c "SELECT host_machine, status, COUNT(*) AS n
              FROM hm_requests
              WHERE ts > NOW() - INTERVAL '$H hours'
              GROUP BY host_machine, status ORDER BY host_machine, status;"
    ;;
  tail)
    N="${2:-20}"
    $PSQL -c "SELECT request_id, ts, host_machine, mapped_model, tier_model, status, duration_ms
              FROM hm_requests ORDER BY ts DESC LIMIT $N;"
    ;;
  *)
    echo "Unknown command: $cmd"
    echo "See script header for commands."
    exit 1
    ;;
esac
