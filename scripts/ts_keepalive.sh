#!/bin/bash
# Tailscale UDP keepalive: maintain NAT mapping for direct P2P connection
# Previous version sent UDP to DERP IPs which are blocked by GFW = useless
# New approach: use tailscale ping to the peer which maintains direct connection
# Run every 1-2 minutes via cron

# Auto-detect peer based on which machine we're running on
if hostname | grep -q 'opc2'; then
    PEER_IP=100.120.104.114  # opc_uname tailscale IP
else
    PEER_IP=100.109.57.26    # opc2_uname tailscale IP
fi

# Ping the peer to keep the direct connection alive
tailscale ping -c 1 $PEER_IP > /dev/null 2>&1

# Log if connection is not direct
RESULT=$(tailscale ping -c 1 $PEER_IP 2>&1)
if echo "$RESULT" | grep -q 'via DERP'; then
    echo "$(date): WARNING - connection via DERP relay (slow)" >> /tmp/ts_keepalive.log
else
    echo "$(date): OK - direct connection active" >> /tmp/ts_keepalive.log
fi