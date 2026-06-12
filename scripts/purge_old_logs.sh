#!/bin/bash
# purge_old_logs.sh — Remove proxy log files older than 7 days
# Usage: Add to crontab: 0 3 * * * /opt/cc-infra/scripts/purge_old_logs.sh
# Keeps: proxy.{date}.log, metrics.{date}.jsonl, error_detail.{date}.jsonl for last 7 days

set -euo pipefail

LOG_DIR="/opt/cc-infra/logs/proxy"
KEEP_DAYS=7

if [ ! -d "$LOG_DIR" ]; then
    echo "[PURGE] Log directory $LOG_DIR not found, skipping"
    exit 0
fi

# Find and delete files older than KEEP_DAYS
deleted=0
kept=0
for f in "$LOG_DIR"/proxy.*.log "$LOG_DIR"/metrics.*.jsonl "$LOG_DIR"/error_detail.*.jsonl; do
    [ -f "$f" ] || continue
    # Check file modification time
    if find "$f" -mtime +$KEEP_DAYS -print 2>/dev/null | grep -q .; then
        echo "[PURGE] Deleting: $f (older than $KEEP_DAYS days)"
        rm -f "$f"
        deleted=$((deleted + 1))
    else
        kept=$((kept + 1))
    fi
done

echo "[PURGE] Done: deleted=$deleted, kept=$kept, threshold=$KEEP_DAYS days"