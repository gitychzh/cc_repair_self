#!/bin/bash
# Tailscale UDP keepalive: maintain NAT mapping for direct P2P connection
# V3: ping ALL peers (LAN + remote) to maintain hole-punching state
# Run every 2 minutes via cron
#
# IMPORTANT: This script NEVER restarts tailscaled. It only pings peers.
# Restarting tailscaled destroys hole-punching progress and creates a vicious cycle.

# All Tailscale peers we want to maintain connections with
PEERS=(
    "100.109.153.83"  # opcsname-1 (remote CC machine = opc_uname, LAN 192.168.1.111)
    "100.121.137.118" # desktop-sgedrr5 (Windows) - MUST keepalive or NAT mapping expires & port rotates
)

LOG="/tmp/ts_keepalive.log"
MAX_LOG_SIZE=50000  # 50KB max log size, auto-truncate

# Auto-truncate log if too large
if [ -f "$LOG" ] && [ $(wc -c < "$LOG") -gt "$MAX_LOG_SIZE" ]; then
    tail -100 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

for PEER_IP in "${PEERS[@]}"; do
    # Skip offline peers (check status first)
    PEER_STATUS=$(tailscale status | grep "$PEER_IP" | head -1)
    if echo "$PEER_STATUS" | grep -q "offline"; then
        echo "$(date '+%H:%M:%S') SKIP $PEER_IP (offline)" >> "$LOG"
        continue
    fi

    # Ping the peer to maintain connection
    RESULT=$(tailscale ping -c 1 "$PEER_IP" 2>&1)

    if echo "$RESULT" | grep -qE 'via (DERP|"sfo"|"tok"|"nue")'; then
        # Connection is via DERP relay - log warning but DO NOT restart
        RELAY=$(echo "$RESULT" | grep -oP 'via "[^"]*"')
        echo "$(date '+%H:%M:%S') WARN $PEER_IP relay $RELAY" >> "$LOG"
    elif echo "$RESULT" | grep -qE 'in \d+ms.*via|is direct'; then
        LATENCY=$(echo "$RESULT" | grep -oP '\d+ms' | head -1)
        echo "$(date '+%H:%M:%S') OK   $PEER_IP direct $LATENCY" >> "$LOG"
    elif echo "$RESULT" | grep -q 'timed out'; then
        echo "$(date '+%H:%M:%S') FAIL $PEER_IP timeout" >> "$LOG"
    else
        echo "$(date '+%H:%M:%S') INFO $PEER_IP $RESULT" >> "$LOG"
    fi
done
