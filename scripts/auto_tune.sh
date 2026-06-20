#!/bin/bash
# auto_tune.sh — Automatic parameter tuning for cc-proxy (R35)
#
# Reads PROXY_HEALTH_SCORES.md (or runs proxy_health_score.py),
# applies TUNE_RULES.md rules, and writes adjustments to configs/NEXT_ROUND.md.
#
# Modes:
#   --dry-run   Show what would be changed, don't write anything
#   --apply     Small safe changes applied directly to docker-compose.yml
#   --suggest   Write suggestions to NEXT_ROUND.md for AI agent / human review
#
# Safety: changes are bounded by TUNE_RULES.md limits.
#         Code changes always require --suggest (never auto-applied).
set -uo pipefail

REPO="/home/opc_uname/cc_ps/cc_repair_self"
COMPOSE="${REPO}/configs/docker-compose.yml"
NEXT_ROUND="${REPO}/configs/NEXT_ROUND.md"
TUNE_RULES="${REPO}/configs/TUNE_RULES.md"
HEALTH_FILE="${REPO}/configs/PROXY_HEALTH_SCORES.md"

MODE="${1:---suggest}"  # --dry-run, --apply, --suggest

ts() { date -Iseconds; }
log() { echo "$(ts) [AUTO-TUNE] $1"; }

# ─── Step 1: Gather health data ───
log "Gathering health data..."

# Run health score if no recent file
if [[ ! -f "$HEALTH_FILE" ]] || [[ $(find "$HEALTH_FILE" -mmin +60 2>/dev/null) ]]; then
    python3 "${REPO}/scripts/proxy_health_score.py" 2>/dev/null || {
        log "WARN: proxy_health_score.py failed, using existing data"
    }
fi

# Extract scores from health file
SCORE01=$(grep '40001.*Score:' "$HEALTH_FILE" 2>/dev/null | grep -oP '\d+\.\d+' | head -1 || echo "0")
SCORE05=$(grep '40005.*Score:' "$HEALTH_FILE" 2>/dev/null | grep -oP '\d+\.\d+' | head -1 || echo "0")
VERDICT=$(grep 'Verdict:' "$HEALTH_FILE" 2>/dev/null | awk '{print $NF}' || echo "UNKNOWN")

# Extract key metrics from 40005
M40005_RATE_429=$(grep '429 rate' "$HEALTH_FILE" 2>/dev/null | grep -A0 '40005' | grep -oP '\d+\.\d+' | head -1 || echo "0")
M40005_TTFB=$(grep 'TTFB avg' "$HEALTH_FILE" 2>/dev/null | tail -1 | grep -oP '\d+' | head -1 || echo "0")
M40005_TIMEOUT=$(grep 'ABORT-NO-FALLBACK' "$HEALTH_FILE" 2>/dev/null | tail -1 | grep -oP '\d+' | head -1 || echo "0")

log "Scores: 40001=${SCORE01}, 40005=${SCORE05}, verdict=${VERDICT}"

# ─── Step 2: Read current params from docker-compose.yml ───
get_env() {
    local port="$1" key="$2"
    # Extract env value from docker-compose.yml for a specific container
    python3 -c "
import yaml, sys
with open('${COMPOSE}') as f:
    dc = yaml.safe_load(f)
for name, svc in dc['services'].items():
    env = svc.get('environment', {})
    port_val = str(env.get('LISTEN_PORT', ''))
    if port_val == '${port}':
        val = env.get('${key}', '')
        print(val)
        break
" 2>/dev/null
}

CURRENT_INTERVAL=$(get_env "40005" "MIN_OUTBOUND_INTERVAL_S")
CURRENT_UPSTREAM_TIMEOUT=$(get_env "40005" "UPSTREAM_TIMEOUT")
CURRENT_PROXY_TIMEOUT=$(get_env "40005" "PROXY_TIMEOUT")
CURRENT_NV_KEYS=$(get_env "40005" "NV_NUM_KEYS")

log "Current 40005: interval=${CURRENT_INTERVAL}s, upstream_timeout=${CURRENT_UPSTREAM_TIMEOUT}s, nv_keys=${CURRENT_NV_KEYS}"

# ─── Step 3: Apply rules ───
CHANGES=()
SUGGESTIONS=()

# Rule: 429_rate > 30% → increase interval
if python3 -c "exit(0 if float('${M40005_RATE_429:-0}') > 30 else 1)" 2>/dev/null; then
    NEW=$(python3 -c "print(min(5.0, float('${CURRENT_INTERVAL:-2.0}') + 0.5))")
    CHANGES+=("MIN_OUTBOUND_INTERVAL_S|${CURRENT_INTERVAL:-2.0}|${NEW}")
    log "RULE: 429_rate=${M40005_RATE_429}% > 30% → interval ${CURRENT_INTERVAL}→${NEW}"
fi

# Rule: 429_rate > 50% → increase interval more
if python3 -c "exit(0 if float('${M40005_RATE_429:-0}') > 50 else 1)" 2>/dev/null; then
    NEW=$(python3 -c "print(min(5.0, float('${CURRENT_INTERVAL:-2.0}') + 1.0))")
    CHANGES+=("MIN_OUTBOUND_INTERVAL_S|${CURRENT_INTERVAL:-2.0}|${NEW}")
    log "RULE: 429_rate=${M40005_RATE_429}% > 50% → interval ${CURRENT_INTERVAL}→${NEW}"
fi

# Rule: 429_rate < 5% → decrease interval
if python3 -c "exit(0 if float('${M40005_RATE_429:-0}') < 5 else 1)" 2>/dev/null; then
    NEW=$(python3 -c "print(max(0.5, float('${CURRENT_INTERVAL:-2.0}') - 0.3))")
    CHANGES+=("MIN_OUTBOUND_INTERVAL_S|${CURRENT_INTERVAL:-2.0}|${NEW}")
    log "RULE: 429_rate=${M40005_RATE_429}% < 5% → interval ${CURRENT_INTERVAL}→${NEW}"
fi

# Rule: Score verdict
if [[ "$VERDICT" == "PROMOTE_40005" ]]; then
    SUGGESTIONS+=("VERSION_PROMOTION: 40005 outperforms 40001 — sync params from 40005→40001")
elif [[ "$VERDICT" == "ROLLBACK_40005" ]]; then
    SUGGESTIONS+=("ROLLBACK: 40005 underperforms — revert 40005 to 40001 baseline params")
fi

# ─── Step 4: Output ───
log "Changes to apply: ${#CHANGES[@]}"
log "Suggestions: ${#SUGGESTIONS[@]}"

if [[ "$MODE" == "--dry-run" ]]; then
    echo ""
    echo "=== DRY RUN: Proposed changes ==="
    for c in "${CHANGES[@]:-}"; do
        IFS='|' read -r key old new <<< "$c"
        echo "  ${key}: ${old} → ${new}"
    done
    for s in "${SUGGESTIONS[@]:-}"; do
        echo "  SUGGEST: ${s}"
    done
    echo ""
    echo "Run with --apply to apply safe param changes, or --suggest to write to NEXT_ROUND.md"

elif [[ "$MODE" == "--apply" ]]; then
    # Apply parameter changes to docker-compose.yml (41005 env only)
    for c in "${CHANGES[@]:-}"; do
        IFS='|' read -r key old new <<< "$c"
        if [[ "$old" != "$new" ]]; then
            log "Applying: ${key} ${old}→${new} (40005 only)"
            # Use sed to update docker-compose.yml
            # This is safe because we only change the 40005 section's env
            python3 -c "
import yaml, sys
with open('${COMPOSE}') as f:
    dc = yaml.safe_load(f)
for name, svc in dc['services'].items():
    env = svc.get('environment', {})
    port_val = str(env.get('LISTEN_PORT', ''))
    if port_val == '40005':
        env['${key}'] = '${new}'
        break
with open('${COMPOSE}', 'w') as f:
    yaml.dump(dc, f, default_flow_style=False, sort_keys=False)
print('Updated ${key}=${new} for 40005')
" 2>/dev/null || log "ERROR: Failed to update ${key}"
        fi
    done

    # Write suggestions too
    for s in "${SUGGESTIONS[@]:-}"; do
        log "SUGGEST (not auto-applied): ${s}"
    done

    log "Changes applied to docker-compose.yml. Rebuild required:"
    log "  cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40005"

elif [[ "$MODE" == "--suggest" ]]; then
    # Write all changes + suggestions to NEXT_ROUND.md
    cat > "$NEXT_ROUND" <<EOF
# NEXT_ROUND — Auto-Tune Suggestions ($(ts))

## Health Scores
- 40001 (stable): ${SCORE01}/100
- 40005 (experiment): ${SCORE05}/100
- Verdict: ${VERDICT}

## Auto-applicable Parameter Changes (40005 only)
EOF
    for c in "${CHANGES[@]:-}"; do
        IFS='|' read -r key old new <<< "$c"
        echo "- ${key}: ${old} → ${new}" >> "$NEXT_ROUND"
    done

    cat >> "$NEXT_ROUND" <<EOF

## Manual Review Required
EOF
    for s in "${SUGGESTIONS[@]:-}"; do
        echo "- ${s}" >> "$NEXT_ROUND"
    done

    cat >> "$NEXT_ROUND" <<EOF

## Action Items
1. Review parameter changes above — if safe, apply to docker-compose.yml and rebuild 40005
2. Review manual suggestions — coordinate with human or AI agent
3. After rebuild, wait 1-2 hours, then run compare_proxies.sh to evaluate
4. If improved for 2+ consecutive hours → version promote to 40001
5. If degraded → rollback 40005 to 40001 baseline

## Commands
\`\`\`bash
# Apply param changes (40005 only)
cd /opt/cc-infra && docker compose up -d --build --force-recreate auth_to_api_40005

# Verify
curl -s http://127.0.0.1:40005/health
curl -s -X POST http://127.0.0.1:40005/v1/messages \\
  -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" \\
  -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50}'

# Evaluate after 1-2 hours
bash scripts/compare_proxies.sh
python3 scripts/proxy_health_score.py
\`\`\`
EOF
    log "Suggestions written to ${NEXT_ROUND}"
fi

log "Done."
