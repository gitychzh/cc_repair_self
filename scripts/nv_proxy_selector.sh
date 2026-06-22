#!/bin/bash
# ── R36.3: NV Proxy Selector ──
# Manages mihomo NV proxy groups for optimal NV API connectivity.
# 1. Queries mihomo API for 5 NV-K groups' active nodes + delays
# 2. Enforces IP diversity — switches duplicate nodes to alternatives
# 3. Validates NV API reachability (GET /v1/models, 0 quota cost)
# 4. Optionally tests NV inference (POST /v1/chat/completions, 1 quota cost)
#
# Usage:
#   nv_proxy_selector.sh [check|validate|test|auto]
#     check    — Check current status + enforce IP diversity
#     validate — Check + validate NV API /v1/models reachability (GET)
#     test     — Check + validate + test NV inference (POST, costs 1 quota)
#     auto     — Validate reachability, then test inference if all reachable
#
# Cron recommended:
#   */5  * * * * nv_proxy_selector.sh check       (IP diversity every 5 min)
#   */30 * * * * nv_proxy_selector.sh validate    (NV reachability every 30 min)
#   0    */2 * * * nv_proxy_selector.sh test       (NV inference every 2 hours)

MIHOMO_API="http://127.0.0.1:9090"
MIHOMO_SECRET="set-your-secret"
NV_API_BASE="https://integrate.api.nvidia.com/v1"
NV_API_KEY="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"
STATUS_FILE="/tmp/nv_proxy_status.json"
LOG_FILE="/tmp/nv_proxy_selector.log"
GET_TIMEOUT=10
POST_TIMEOUT=30

log() { echo "[$(date '+%H:%M:%S')] $1" >> "$LOG_FILE"; echo "[$(date '+%H:%M:%S')] $1"; }

# ── Get all proxy data from mihomo API ──
get_mihomo_data() {
    curl -s -H "Authorization: Bearer $MIHOMO_SECRET" "$MIHOMO_API/proxies"
}

# ── URL-encode a group name ──
url_encode() { python3 -c "import urllib.parse; print(urllib.parse.quote('$1'))"; }

# ── Switch a proxy group to a specific node ──
switch_group() {
    local group="$1" node="$2"
    local encoded=$(url_encode "$group")
    curl -s -X PUT -H "Authorization: Bearer $MIHOMO_SECRET" \
        -H "Content-Type: application/json" \
        "$MIHOMO_API/proxies/$encoded" \
        -d "{\"name\":\"$node\"}" 2>/dev/null
}

# ── Check: Get status + enforce IP diversity ──
check_status() {
    log "=== Checking NV-K group status ==="

    # Use python3 to extract all data at once (avoids bash string issues with emoji/unicode)
    local data=$(get_mihomo_data)
    if [ -z "$data" ]; then
        log "ERROR: mihomo API unreachable"
        return 1
    fi

    # Extract active nodes, delays, and members for each group
    local analysis=$(python3 << 'PYEOF'
import json, sys, urllib.request

MIHOMO_API = "http://127.0.0.1:9090"
MIHOMO_SECRET = "set-your-secret"

req = urllib.request.Request(
    f"{MIHOMO_API}/proxies",
    headers={"Authorization": f"Bearer {MIHOMO_SECRET}"}
)
with urllib.request.urlopen(req) as resp:
    proxies = json.load(resp)["proxies"]

groups = ["♻️US-NV-K1", "♻️US-NV-K2", "♻️US-NV-K3", "♻️US-NV-K4", "♻️US-NV-K5"]
ports = [7894, 7895, 7896, 7897, 7899]

# Collect active nodes
active_nodes = {}
for i, g in enumerate(groups):
    info = proxies.get(g, {})
    now = info.get("now", "")
    members = info.get("all", [])

    # Get delay of active node
    now_info = proxies.get(now, {})
    h = now_info.get("history", [])
    delay = h[-1].get("delay", 0) if h else 0

    # Find all reachable alternatives with delays
    alternatives = []
    for m in members:
        m_info = proxies.get(m, {})
        m_h = m_info.get("history", [])
        m_delay = m_h[-1].get("delay", 0) if m_h else 0
        if m_delay > 0:  # reachable
            alternatives.append((m, m_delay))

    alternatives.sort(key=lambda x: x[1])  # sort by delay

    active_nodes[g] = {
        "key": i + 1,
        "port": ports[i],
        "active": now,
        "delay": delay,
        "reachable": delay > 0,
        "pool_size": len(members),
        "alternatives": alternatives,
    }

    status = "REACHABLE" if delay > 0 else "UNREACHABLE"
    print(f"  K{i+1} ({ports[i]}): {status} active={now}, delay={delay}ms, pool={len(members)}, alternatives={len(alternatives)}")

# ── IP diversity enforcement ──
print("--- Checking IP diversity ---")
used_nodes = set()
switches_needed = []

for g in groups:
    info = active_nodes[g]
    node = info["active"]
    key = info["key"]
    port = info["port"]

    if node in used_nodes and node != "":
        print(f"  K{key} ({port}): DUPLICATE — {node} already used by another group")
        # Find best alternative not in used_nodes
        best_alt = None
        for alt_name, alt_delay in info["alternatives"]:
            if alt_name not in used_nodes:
                best_alt = alt_name
                break
        if best_alt:
            print(f"  K{key} ({port}): Will switch to {best_alt}")
            switches_needed.append((g, best_alt))
            used_nodes.add(best_alt)
        else:
            print(f"  K{key} ({port}): WARNING — no unique alternative available")
            used_nodes.add(node)
    elif node != "":
        used_nodes.add(node)
        print(f"  K{key} ({port}): UNIQUE — {node}")
    else:
        print(f"  K{key} ({port}): NO active node")

print(f"\nUnique nodes: {len(used_nodes)}/5 groups")

# Print switch commands
if switches_needed:
    print("--- Switching ---")
    for g, alt in switches_needed:
        encoded = urllib.parse.quote(g)
        print(f"SWITCH:{g}:{alt}:{encoded}")
PYEOF
)

    log "$analysis"

    # Execute switches
    local switches=$(echo "$analysis" | grep "^SWITCH:" | cut -d: -f2-4)
    if [ -n "$switches" ]; then
        while IFS=: read -r group alt encoded; do
            log "Switching $group to $alt..."
            local result=$(switch_group "$group" "$alt")
            log "  Result: $result"
        done <<< "$switches"
    fi

    # Write status JSON
    python3 << 'PYEOF'
import json, datetime, urllib.request

MIHOMO_API = "http://127.0.0.1:9090"
MIHOMO_SECRET = "set-your-secret"

req = urllib.request.Request(
    f"{MIHOMO_API}/proxies",
    headers={"Authorization": f"Bearer {MIHOMO_SECRET}"}
)
with urllib.request.urlopen(req) as resp:
    proxies = json.load(resp)["proxies"]

groups = ["♻️US-NV-K1", "♻️US-NV-K2", "♻️US-NV-K3", "♻️US-NV-K4", "♻️US-NV-K5"]
ports = [7894, 7895, 7896, 7897, 7899]

status = {
    "timestamp": datetime.datetime.now().isoformat(),
    "groups": [],
    "nv_api_reachable": None,
    "nv_inference_ok": None,
}

unique_nodes = set()
for i, g in enumerate(groups):
    info = proxies.get(g, {})
    now = info.get("now", "")
    unique_nodes.add(now)
    status["groups"].append({
        "key": i + 1,
        "port": ports[i],
        "group": g,
        "active_node": now,
        "pool_size": len(info.get("all", [])),
    })

status["unique_count"] = len(unique_nodes)

with open("/tmp/nv_proxy_status.json", "w") as f:
    json.dump(status, f, indent=2)

print(f"Status written to /tmp/nv_proxy_status.json ({len(unique_nodes)} unique nodes)")
PYEOF
}

# ── Validate: Test NV API /v1/models reachability ──
validate_reachability() {
    log "=== Validating NV API reachability ==="
    local ports=(7894 7895 7896 7897 7899)
    local reachable=0
    local unreachable=0
    local reachable_list=""
    local unreachable_list=""

    for port in "${ports[@]}"; do
        local http_code=$(curl -s -x http://127.0.0.1:$port \
            "$NV_API_BASE/models" \
            --max-time $GET_TIMEOUT \
            -o /dev/null -w "%{http_code}" 2>/dev/null || echo "000")
        local time_s=$(curl -s -x http://127.0.0.1:$port \
            "$NV_API_BASE/models" \
            --max-time $GET_TIMEOUT \
            -o /dev/null -w "%{time_total}" 2>/dev/null || echo "timeout")

        if [ "$http_code" = "200" ]; then
            log "  Port $port: REACHABLE (HTTP 200, ${time_s}s)"
            reachable=$((reachable + 1))
            reachable_list="$reachable_list $port"
        else
            log "  Port $port: UNREACHABLE (HTTP $http_code, ${time_s}s)"
            unreachable=$((unreachable + 1))
            unreachable_list="$unreachable_list $port"
        fi
    done

    log "Reachable: $reachable/5 ports$reachable_list"
    if [ "$unreachable" -gt 0 ]; then
        log "Unreachable: $unreachable ports$unreachable_list"
    fi

    # Update status file
    python3 -c "
import json
with open('$STATUS_FILE') as f:
    s = json.load(f)
s['nv_api_reachable'] = $reachable == 5
s['reachable_count'] = $reachable
s['unreachable_count'] = $unreachable
with open('$STATUS_FILE', 'w') as f:
    json.dump(s, f, indent=2)
" 2>/dev/null

    [ "$reachable" -eq 5 ] && return 0 || return 1
}

# ── Test: Test NV inference ──
test_inference() {
    log "=== Testing NV API inference ==="
    local port=7894  # Use K1 for test
    local response=$(curl -s -x http://127.0.0.1:$port \
        -X POST "$NV_API_BASE/chat/completions" \
        -H "Authorization: Bearer $NV_API_KEY" \
        -H "Content-Type: application/json" \
        -d '{"model":"z-ai/glm-5.1","messages":[{"role":"user","content":"1"}],"max_tokens":1}' \
        --max-time $POST_TIMEOUT \
        -w "\nHTTP_CODE:%{http_code}" 2>/dev/null)

    local http_code=$(echo "$response" | grep "HTTP_CODE:" | cut -d: -f2)

    if [ "$http_code" = "200" ]; then
        log "  NV inference: OK (HTTP 200)"
        python3 -c "
import json
with open('$STATUS_FILE') as f:
    s = json.load(f)
s['nv_inference_ok'] = True
s['nv_inference_time'] = '$(date +%s)'
with open('$STATUS_FILE', 'w') as f:
    json.dump(s, f, indent=2)
" 2>/dev/null
        return 0
    else
        log "  NV inference: FAILED (HTTP $http_code or timeout)"
        python3 -c "
import json
with open('$STATUS_FILE') as f:
    s = json.load(f)
s['nv_inference_ok'] = False
with open('$STATUS_FILE', 'w') as f:
    json.dump(s, f, indent=2)
" 2>/dev/null
        return 1
    fi
}

# ── Auto ──
auto() {
    check_status
    if validate_reachability; then
        test_inference
    else
        log "NV API unreachable, skipping inference test"
    fi
}

# ── Main ──
case "${1:-auto}" in
    check)    check_status ;;
    validate) check_status; validate_reachability ;;
    test)     check_status; validate_reachability; test_inference ;;
    auto)     auto ;;
    *)
        echo "Usage: $0 [check|validate|test|auto]"
        echo "  check    — Check status + enforce IP diversity"
        echo "  validate — Check + validate NV API reachability"
        echo "  test     — Check + validate + test NV inference"
        echo "  auto     — Validate, then test if reachable"
        exit 1 ;;
esac
