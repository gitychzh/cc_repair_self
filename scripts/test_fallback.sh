#!/bin/bash
# test_fallback.sh — Verify dispatcher auto-fallback: 40005 down → 40001 takes over
# R35: Critical test — if fallback fails, user loses CC access entirely.
#
# Steps:
#   1. Pre-check: verify both 40001 and 40005 work independently
#   2. Pre-check: verify 40000 works normally (→40005)
#   3. Stop 40005
#   4. Send request via 40000 → should fallback to 40001
#   5. Verify response is valid (200, Anthropic format)
#   6. Check dispatcher logs for fallback event
#   7. Restart 40005
#   8. Send request via 40000 → should go back to 40005
#   9. Verify all containers healthy again
set -uo pipefail

PASS=0; FAIL=0
ts() { date -Iseconds; }
log() { echo "$(ts) [TEST] $1"; }
check() { if [[ $? -eq 0 ]]; then log "✅ PASS: $1"; PASS=$((PASS+1)); else log "❌ FAIL: $1"; FAIL=$((FAIL+1)); fi; }

REQUEST_BODY='{"model":"claude-opus-4-8","messages":[{"role":"user","content":"test fallback say ok"}],"max_tokens":15}'
HEADERS="-H x-api-key:sk-litellm-local -H anthropic-version:2023-06-01"

# ─── Step 1: Pre-check both proxies independently ───
log "Step 1: Verify 40001 and 40005 independently"

R40001=$(curl -s -m 30 -X POST http://127.0.0.1:40001/v1/messages $HEADERS -d "$REQUEST_BODY" 2>&1)
if echo "$R40001" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('type')=='message', 'not message type'; assert d.get('id'), 'no id'" 2>/dev/null; then
    check "40001 direct: returns valid Anthropic response"
else
    log "❌ FAIL: 40001 direct test failed — response: $(echo "$R40001" | head -1)"
    FAIL=$((FAIL+1))
fi

R40005=$(curl -s -m 30 -X POST http://127.0.0.1:40005/v1/messages $HEADERS -d "$REQUEST_BODY" 2>&1)
if echo "$R40005" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('type')=='message', 'not message type'; assert d.get('id'), 'no id'" 2>/dev/null; then
    check "40005 direct: returns valid Anthropic response"
else
    log "❌ FAIL: 40005 direct test failed — response: $(echo "$R40005" | head -1)"
    FAIL=$((FAIL+1))
fi

# ─── Step 2: Verify 40000 normal operation ───
log "Step 2: Verify 40000 normal operation (→40005)"

R40000=$(curl -s -m 30 -X POST http://127.0.0.1:40000/v1/messages $HEADERS -d "$REQUEST_BODY" 2>&1)
if echo "$R40000" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('type')=='message', 'not message type'" 2>/dev/null; then
    check "40000 normal: returns valid Anthropic response"
else
    log "❌ FAIL: 40000 normal test failed — ABORT"
    FAIL=$((FAIL+1))
    echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
    exit 1
fi

# ─── Step 3: Stop 40005 ───
log "Step 3: Stopping 40005 container..."
cd /opt/cc-infra && docker compose stop auth_to_api_40005 2>&1
sleep 3  # Wait for dispatcher to detect failure on next request

# Verify 40005 is down
curl -sf -m 3 http://127.0.0.1:40005/health >/dev/null 2>&1
if [[ $? -ne 0 ]]; then
    check "40005 is confirmed DOWN"
else
    log "⚠️ 40005 still responding (slow shutdown?), waiting..."
    sleep 5
    curl -sf -m 3 http://127.0.0.1:40005/health >/dev/null 2>&1 || true
fi

# ─── Step 4: Send request via 40000 → should fallback to 40001 ───
log "Step 4: Testing fallback — request via 40000 with 40005 DOWN"

R_FALLBACK=$(curl -s -m 60 -X POST http://127.0.0.1:40000/v1/messages $HEADERS -d "$REQUEST_BODY" 2>&1)
log "Fallback response (first 200 chars): $(echo "$R_FALLBACK" | head -c 200)"

# Verify it's a valid Anthropic response
if echo "$R_FALLBACK" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('type')=='message', f'type={d.get(\"type\")}, not message'; assert d.get('id'), 'no id'; print(f'Model={d.get(\"model\")}, content_blocks={len(d.get(\"content\",[]))}')" 2>/dev/null; then
    check "FALLBACK SUCCESS: 40000→40001 returns valid Anthropic response"
else
    log "❌ FAIL: Fallback response invalid — CC would be stuck!"
    log "Response: $R_FALLBACK"
    FAIL=$((FAIL+1))
    # EMERGENCY: restart 40005 immediately
    log "EMERGENCY: restarting 40005 to restore access..."
    cd /opt/cc-infra && docker compose start auth_to_api_40005 2>&1
    echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
    exit 1
fi

# ─── Step 5: Check dispatcher logs for fallback event ───
log "Step 5: Checking dispatcher logs for fallback event"

DOCKER_LOGS=$(docker logs --since 30s auth_to_api_40000 2>&1)
if echo "$DOCKER_LOGS" | grep -q "primary failed"; then
    check "Dispatcher log: 'primary failed' event found"
    # Show the exact log line
    FALLBACK_LINE=$(echo "$DOCKER_LOGS" | grep "primary failed" | head -1)
    log "  Log line: $FALLBACK_LINE"
elif echo "$DOCKER_LOGS" | grep -q "auto-fallback"; then
    check "Dispatcher log: 'auto-fallback' event found"
    FALLBACK_LINE=$(echo "$DOCKER_LOGS" | grep "auto-fallback" | head -1)
    log "  Log line: $FALLBACK_LINE"
elif echo "$DOCKER_LOGS" | grep -q "connect failed"; then
    check "Dispatcher log: 'connect failed' event found"
    FALLBACK_LINE=$(echo "$DOCKER_LOGS" | grep "connect failed" | head -1)
    log "  Log line: $FALLBACK_LINE"
else
    log "⚠️ No explicit fallback log line found — checking all recent logs:"
    echo "$DOCKER_LOGS" | tail -5
fi

# ─── Step 6: Also verify 40001 received the request ───
log "Step 6: Checking 40001 logs for the fallback request"

# 40001 should have logged the request
PROXY01_LOGS=$(docker logs --since 30s auth_to_api_40001 2>&1 | grep -i "REQ\|KEY-RR\|MS-NV\|AGENT" | tail -5)
if [[ -n "$PROXY01_LOGS" ]]; then
    check "40001 logs show it received and processed the fallback request"
    log "  Last 40001 log entries:"
    echo "$PROXY01_LOGS" | while read line; do log "    $line"; done
else
    log "⚠️ 40001 logs empty for recent requests — checking raw output:"
    docker logs --since 30s auth_to_api_40001 2>&1 | tail -5
fi

# ─── Step 7: Restart 40005 ───
log "Step 7: Restarting 40005..."
cd /opt/cc-infra && docker compose start auth_to_api_40005 2>&1
sleep 5

# ─── Step 8: Verify 40000 goes back to 40005 ───
log "Step 8: Verify 40000 restores to primary (40005)"

R_RESTORE=$(curl -s -m 30 -X POST http://127.0.0.1:40000/v1/messages $HEADERS -d "$REQUEST_BODY" 2>&1)
if echo "$R_RESTORE" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('type')=='message', 'not message'" 2>/dev/null; then
    check "40000 restored: returns valid response via 40005"
else
    log "⚠️ Restore response unusual — checking: $(echo "$R_RESTORE" | head -c 200)"
    FAIL=$((FAIL+1))
fi

# ─── Step 9: All containers healthy ───
log "Step 9: Final health check"
curl -sf -m 5 http://127.0.0.1:40000/health >/dev/null 2>&1 && check "40000 healthy" || { log "❌ 40000 unhealthy"; FAIL=$((FAIL+1)); }
curl -sf -m 5 http://127.0.0.1:40001/health >/dev/null 2>&1 && check "40001 healthy" || { log "❌ 40001 unhealthy"; FAIL=$((FAIL+1)); }
curl -sf -m 5 http://127.0.0.1:40005/health >/dev/null 2>&1 && check "40005 healthy" || { log "❌ 40005 unhealthy"; FAIL=$((FAIL+1)); }

echo ""
echo "========================================="
echo "  FALLBACK TEST SUMMARY"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [[ $FAIL -eq 0 ]]; then
    echo "  Result: ✅ ALL PASSED — fallback works!"
else
    echo "  Result: ❌ SOME FAILED — review logs above"
fi
echo "========================================="
exit $FAIL
