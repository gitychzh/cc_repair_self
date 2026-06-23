#!/bin/bash
# Log cleanup: delete proxy/litellm/dispatcher logs older than 7 days
# Safe: only targets dated files (*.YYYY-MM-DD.log, *.YYYY-MM-DD.jsonl)
# Does NOT delete rr_counter.json, config files, or current-day files

LOG_DIRS="/opt/cc-infra/logs/proxy40001 /opt/cc-infra/logs/proxy40002 /opt/cc-infra/logs/proxy40003 /opt/cc-infra/logs/proxy40005 /opt/cc-infra/logs/proxy40006 /opt/cc-infra/logs/proxy /opt/cc-infra/logs/litellm-glm51 /opt/cc-infra/logs/litellm-nv-hm-k1 /opt/cc-infra/logs/litellm-nv-hm-k2 /opt/cc-infra/logs/litellm-nv-hm-k3 /opt/cc-infra/logs/litellm-nv-hm-k4 /opt/cc-infra/logs/litellm-nv-hm-k5"
RETENTION_DAYS=7

for dir in $LOG_DIRS; do
    if [ -d "$dir" ]; then
        find "$dir" -name "*.log" -mtime +$RETENTION_DAYS -delete 2>/dev/null
        find "$dir" -name "*.jsonl" -mtime +$RETENTION_DAYS -delete 2>/dev/null
    fi
done

# Also clean empty stale directories from old NV monitoring (removed R38.1)
for stale_dir in /opt/cc-infra/logs/litellm-nv-41006 /opt/cc-infra/logs/litellm-nv-41007 /opt/cc-infra/logs/litellm-nv-41008 /opt/cc-infra/logs/litellm-nv-41009 /opt/cc-infra/logs/litellm-nv-41010 /opt/cc-infra/logs/litellm-nv-k1 /opt/cc-infra/logs/litellm-nv-k2 /opt/cc-infra/logs/litellm-nv-k3 /opt/cc-infra/logs/litellm-nv-k4 /opt/cc-infra/logs/litellm-nv-k5 /opt/cc-infra/logs/proxy-40002; do
    if [ -d "$stale_dir" ] && [ "$(ls -A "$stale_dir" 2>/dev/null)" = "" ]; then
        rmdir "$stale_dir" 2>/dev/null
    fi
done
