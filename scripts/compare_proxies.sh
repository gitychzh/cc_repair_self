#!/bin/bash
# compare_proxies.sh — Compare metrics between 40005 (primary/experiment) and 40001 (fallback/stable)
# R35: Self-optimization framework — data-driven comparison tool
#
# Usage:
#   bash scripts/compare_proxies.sh            # Today's metrics
#   bash scripts/compare_proxies.sh 2026-06-20  # Specific date
#   bash scripts/compare_proxies.sh last2h       # Last 2 hours (today)
#
# Output: Structured comparison table + verdict
set -uo pipefail

REPO="/home/opc_uname/cc_ps/cc_repair_self"
DEPLOY="/opt/cc-infra"

# Determine date
if [[ -n "${1:-}" ]]; then
    if [[ "$1" == "last2h" ]]; then
        DATE=$(date -Idate)
        TIME_FILTER="--last2h"
    else
        DATE="$1"
        TIME_FILTER=""
    fi
else
    DATE=$(date -Idate)
    TIME_FILTER=""
fi

# Metrics file paths
M40001="${DEPLOY}/logs/proxy40001/metrics.${DATE}.jsonl"
M40005="${DEPLOY}/logs/proxy40005/metrics.${DATE}.jsonl"

echo "=== Proxy Comparison: ${DATE} ==="
echo "40001 (fallback/stable): ${M40001}"
echo "40005 (primary/experiment): ${M40005}"
echo ""

# Check file existence
F40001=0; F40005=0
[[ -f "$M40001" ]] && F40001=1
[[ -f "$M40005" ]] && F40005=1

if [[ $F40001 -eq 0 && $F40005 -eq 0 ]]; then
    echo "No metrics files found for ${DATE}. Cannot compare."
    exit 1
fi

# Helper: extract stats from a metrics file
extract_stats() {
    local FILE="$1"
    local LABEL="$2"
    local FILTER="$3"

    if [[ ! -f "$FILE" ]]; then
        echo "${LABEL}: NO DATA"
        return
    fi

    # Time filter
    local DATA="$FILE"
    if [[ "$FILTER" == "--last2h" ]]; then
        local NOW=$(date +%s)
        local TWO_H_AGO=$((NOW - 7200))
        # Filter: keep only entries with timestamp within last 2 hours
        local TMP=$(mktemp)
        python3 -c "
import json, sys, datetime
now = datetime.datetime.now()
two_h = now - datetime.timedelta(hours=2)
with open('$FILE') as f:
    for line in f:
        try:
            entry = json.loads(line)
            ts = entry.get('timestamp','')
            if ts:
                dt = datetime.datetime.fromisoformat(ts)
                if dt >= two_h:
                    sys.stdout.write(line)
        except: pass
" > "$TMP" 2>/dev/null
        DATA="$TMP"
    fi

    python3 -c "
import json, sys

file = '$DATA'
label = '$LABEL'

total = 0; success = 0; errors = 0
status_429 = 0; status_502 = 0; status_400 = 0; other_err = 0
ttfb_list = []; duration_list = []
ms_slots = 0; nv_slots = 0
key_cycles = 0; abort_no_fallback = 0

with open(file) as f:
    for line in f:
        try:
            e = json.loads(line)
        except:
            continue
        total += 1
        st = e.get('status', 0)
        if st == 200:
            success += 1
            ttfb = e.get('ttfb_ms')
            dur = e.get('duration_ms')
            if ttfb and ttfb > 0:
                ttfb_list.append(ttfb)
            if dur and dur > 0:
                duration_list.append(dur)
            # Upstream type
            ut = e.get('upstream_type', 'ms')
            if ut == 'ms':
                ms_slots += 1
            elif ut == 'nv':
                nv_slots += 1
            # Key cycles
            kc = e.get('key_cycle_429s_before_success', 0)
            if kc and kc > 0:
                key_cycles += 1
        elif st == 429:
            status_429 += 1
            errors += 1
            if e.get('error_type') == '429_all_transient' or 'ABORT' in str(e.get('error_message','')):
                abort_no_fallback += 1
        elif st == 502:
            status_502 += 1
            errors += 1
        elif st == 400:
            status_400 += 1
            errors += 1
        else:
            if st > 0:
                other_err += 1
                errors += 1

def avg(lst): return sum(lst)/len(lst) if lst else 0
def p50(lst):
    if not lst: return 0
    s = sorted(lst)
    return s[len(s)//2]
def p95(lst):
    if not lst: return 0
    s = sorted(lst)
    idx = int(len(s) * 0.95)
    return s[min(idx, len(s)-1)]

print(f'{label}:')
print(f'  Total requests:    {total}')
print(f'  Success (200):     {success} ({success/total*100:.1f}%)' if total else f'  Success: 0')
print(f'  Errors:            {errors} ({errors/total*100:.1f}%)' if total else f'  Errors: 0')
print(f'    429 rate_limit:  {status_429} ({status_429/total*100:.1f}%)' if total else f'    429: 0')
print(f'    ABORT-NO-FB:     {abort_no_fallback}')
print(f'    502 upstream:    {status_502}')
print(f'    400 bad_request: {status_400}')
print(f'    other:           {other_err}')
print(f'  TTFB avg/p50/p95:  {avg(ttfb_list):.0f}/{p50(ttfb_list):.0f}/{p95(ttfb_list):.0f} ms')
print(f'  Duration avg/p50:  {avg(duration_list):.0f}/{p50(duration_list):.0f} ms')
print(f'  MS slots:          {ms_slots}')
print(f'  NV slots:          {nv_slots}')
print(f'  Key cycles (429→success): {key_cycles}')
print(f'  NV ratio:          {nv_slots/(ms_slots+nv_slots)*100:.1f}%' if (ms_slots+nv_slots) else f'  NV ratio: N/A')
" 2>/dev/null

    # Cleanup temp file
    if [[ "$FILTER" == "--last2h" && -n "$TMP" ]]; then
        rm -f "$TMP"
    fi
}

echo "--- 40001 (Fallback/Stable) ---"
extract_stats "$M40001" "40001" "$TIME_FILTER"

echo ""
echo "--- 40005 (Primary/Experiment) ---"
extract_stats "$M40005" "40005" "$TIME_FILTER"

echo ""
echo "=== Verdict ==="
python3 -c "
import json, sys

def load_stats(file):
    total=0; success=0; s429=0; s502=0; ttfb_list=[]; ms=0; nv=0
    if not file:
        return {'total':0,'success_rate':0,'429_rate':0,'502_rate':0,'ttfb_avg':0,'nv_ratio':0}
    try:
        with open(file) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except: continue
                total += 1
                st = e.get('status',0)
                if st == 200:
                    success += 1
                    t = e.get('ttfb_ms')
                    if t and t > 0: ttfb_list.append(t)
                    ut = e.get('upstream_type','ms')
                    if ut == 'ms': ms += 1
                    elif ut == 'nv': nv += 1
                elif st == 429: s429 += 1
                elif st == 502: s502 += 1
    except: pass
    return {
        'total': total,
        'success_rate': success/total*100 if total else 0,
        '429_rate': s429/total*100 if total else 0,
        '502_rate': s502/total*100 if total else 0,
        'ttfb_avg': sum(ttfb_list)/len(ttfb_list) if ttfb_list else 0,
        'nv_ratio': nv/(ms+nv)*100 if (ms+nv) else 0,
    }

s01 = load_stats('$M40001')
s05 = load_stats('$M40005')

# Health score: higher = better
# Score = 100 - (429_rate * 3) - (502_rate * 2) - (ttfb_avg / 100) + (nv_ratio / 5)
def score(s):
    base = 100
    base -= s['429_rate'] * 3
    base -= s['502_rate'] * 2
    base -= s['ttfb_avg'] / 100
    base += s['nv_ratio'] / 5  # NV interleaving bonus
    return max(0, min(100, base))

score01 = score(s01)
score05 = score(s05)

print(f'40001 health score: {score01:.1f}/100')
print(f'40005 health score: {score05:.1f}/100')

if s05['total'] == 0:
    print('40005 has NO traffic → cannot evaluate. Deploy experiment first.')
elif score05 >= score01 + 5:
    print('✅ 40005 OUTPERFORMS 40001 → eligible for VERSION PROMOTION')
elif score05 < score01 - 10:
    print('⚠️ 40005 UNDERPERFORMS → consider ROLLBACK')
else:
    print('📊 Similar performance → continue observing')
" 2>/dev/null

echo ""
echo "Run: bash scripts/proxy_health_score.py for detailed health report"
