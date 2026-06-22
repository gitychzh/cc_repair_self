#!/usr/bin/env python3
# ── R36.3: NV Proxy Selector ──
# Tests all US proxy nodes against NV API (integrate.api.nvidia.com/v1/models),
# ranks by latency, assigns top5 lowest-latency nodes to K1-K5.
# Each key gets a different node → different exit IP → IP diversity guaranteed.
#
# Logic:
#   1. Query mihomo API for all US nodes in nv-us-provider pool
#   2. Test each node's latency to integrate.api.nvidia.com/v1/models (GET, 0 quota)
#   3. Sort by latency, pick top5 unique nodes
#   4. Assign top5 to ♻️US-NV-K1~K5 via mihomo PUT API (type:select groups)
#   5. Optionally test NV inference (POST /v1/chat/completions, 1 quota)
#
# Usage:
#   nv_proxy_selector.sh           — Select top5 by NV latency, assign to K1-K5
#   nv_proxy_selector.sh --test    — Select + test NV inference
#   nv_proxy_selector.sh --status  — Show current K1-K5 assignments only
#
# Cron recommended:
#   */5  * * * *  nv_proxy_selector.sh          (re-select every 5 min)
#   0    */2 * * *  nv_proxy_selector.sh --test  (test inference every 2 hours)

import json, sys, os, datetime, urllib.request, urllib.parse, urllib.error, time

MIHOMO_API = "http://127.0.0.1:9090"
MIHOMO_SECRET = "set-your-secret"
NV_API_BASE = "https://integrate.api.nvidia.com/v1"
NV_API_KEY = "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"
STATUS_FILE = "/tmp/nv_proxy_status.json"
LOG_FILE = "/tmp/nv_proxy_selector.log"
NV_K_GROUPS = ["♻️US-NV-K1", "♻️US-NV-K2", "♻️US-NV-K3", "♻️US-NV-K4", "♻️US-NV-K5"]
NV_K_PORTS = [7894, 7895, 7896, 7897, 7899]
TEST_TIMEOUT_MS = 15000  # 15s for NV API GET latency test

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def mihomo_request(path, method="GET", data=None):
    url = f"{MIHOMO_API}{path}"
    headers = {"Authorization": f"Bearer {MIHOMO_SECRET}"}
    if data:
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method=method)
    else:
        req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if not body:
                return {}  # 204 No Content (successful PUT for group switch)
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return {}  # PUT success returns 204
        log(f"mihomo API error: {e.code} {e.reason}")
        return None
    except Exception as e:
        log(f"mihomo API error: {e}")
        return None

def get_nv_nodes():
    """Get all US nodes from nv-us-provider pool."""
    data = mihomo_request("/proxies")
    if not data:
        return []
    proxies = data.get("proxies", {})
    # Find any NV-K group (all share the same pool via nv-us-provider)
    # Use K5 since it has all nodes
    k5 = proxies.get(NV_K_GROUPS[4], {})
    members = k5.get("all", [])
    if not members:
        # Try the direct ♻️US-NV group (fallback)
        us_nv = proxies.get("♻️US-NV", {})
        members = us_nv.get("all", [])
    return members

def test_nv_latency(node_name):
    """Test a single node's latency to NV API /v1/models via mihomo API."""
    # We can't test individual nodes through mihomo API
    # Instead, test the whole group and parse results
    # This is done in batch via test_all_nv_latency()
    pass

def test_all_nv_latency():
    """Test all US nodes against NV API, return sorted by latency."""
    # Use mihomo's group delay test API against NV API endpoint
    # This tests all members of a group in parallel
    # We need a group that contains all US nodes

    # First, try using K5 (should have all nodes if type:select with nv-us-provider)
    # If K5 doesn't have members yet (fresh start), use ♻️US-NV as fallback
    encoded = urllib.parse.quote(NV_K_GROUPS[4])

    log(f"Testing all US nodes against NV API ({NV_API_BASE}/models)...")
    result = mihomo_request(
        f"/group/{encoded}/delay?url={NV_API_BASE}/models&timeout={TEST_TIMEOUT_MS}"
    )

    if not result or len(result) < 3:
        # Try ♻️US-NV fallback
        encoded_fallback = urllib.parse.quote("♻️US-NV")
        log("K5 has no members, trying ♻️US-NV fallback...")
        result = mihomo_request(
            f"/group/{encoded_fallback}/delay?url={NV_API_BASE}/models&timeout={TEST_TIMEOUT_MS}"
        )

    if not result:
        log("ERROR: Failed to test NV latency via mihomo API")
        return []

    # Parse results: sort by latency, filter reachable (delay > 0 and < timeout)
    reachable = []
    for name, delay in result.items():
        if 0 < delay < TEST_TIMEOUT_MS:
            reachable.append((name, delay))

    reachable.sort(key=lambda x: x[1])  # sort ascending by latency

    log(f"Tested {len(result)} nodes, {len(reachable)} reachable to NV API")
    for i, (name, delay) in enumerate(reachable[:10]):
        log(f"  #{i+1}: {delay}ms — {name}")

    return reachable

def assign_top5(top5):
    """Assign top5 nodes to K1-K5 via mihomo PUT API."""
    if len(top5) < 5:
        log(f"WARNING: Only {len(top5)} reachable nodes, need 5 for K1-K5")
        # Fill remaining slots with whatever we have
        while len(top5) < 5:
            top5.append(("unavailable", 0))

    for i, (node_name, delay) in enumerate(top5[:5]):
        group = NV_K_GROUPS[i]
        encoded = urllib.parse.quote(group)
        port = NV_K_PORTS[i]

        if node_name == "unavailable":
            log(f"  K{i+1} ({port}): NO available node")
            continue

        result = mihomo_request(f"/proxies/{encoded}", method="PUT", data={"name": node_name})
        log(f"  K{i+1} ({port}): assigned {node_name} ({delay}ms)")

def show_status():
    """Show current K1-K5 assignments."""
    data = mihomo_request("/proxies")
    if not data:
        log("ERROR: mihomo API unreachable")
        return
    proxies = data.get("proxies", {})
    log("=== Current NV-K assignments ===")
    for i, group in enumerate(NV_K_GROUPS):
        info = proxies.get(group, {})
        now = info.get("now", "")
        pool = len(info.get("all", []))
        log(f"  K{i+1} ({NV_K_PORTS[i]}): {now} (pool={pool})")

def validate_nv_reachability():
    """Quick check: test each K port against NV API via curl."""
    import subprocess
    log("=== Validating NV API reachability per port ===")
    all_ok = True
    for port in NV_K_PORTS:
        try:
            proc = subprocess.run(
                ["curl", "-s", "-x", f"http://127.0.0.1:{port}",
                 f"{NV_API_BASE}/models", "--max-time", "10",
                 "-o", "/dev/null", "-w", "%{http_code} %{time_total}"],
                capture_output=True, text=True, timeout=15
            )
            parts = proc.stdout.strip().split()
            http_code = parts[0] if parts else "000"
            time_s = parts[1] if len(parts) > 1 else "?"
            ok = http_code == "200"
            status = "OK" if ok else "FAIL"
            log(f"  Port {port}: {status} (HTTP {http_code}, {time_s}s)")
            if not ok:
                all_ok = False
        except Exception as e:
            log(f"  Port {port}: ERROR ({e})")
            all_ok = False
    return all_ok

def test_nv_inference():
    """Test NV inference via port 7894 (costs 1 quota call)."""
    import subprocess
    log("=== Testing NV inference ===")
    try:
        proc = subprocess.run(
            ["curl", "-s", "-x", "http://127.0.0.1:7894",
             "-X", "POST", f"{NV_API_BASE}/chat/completions",
             "-H", f"Authorization: Bearer {NV_API_KEY}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"model": "z-ai/glm-5.1",
                              "messages": [{"role": "user", "content": "1"}],
                              "max_tokens": 1}),
             "--max-time", "30", "-w", "\nHTTP:%{http_code}"],
            capture_output=True, text=True, timeout=35
        )
        http_code_line = [l for l in proc.stdout.split("\n") if l.startswith("HTTP:")]
        http_code = http_code_line[0].split(":")[1] if http_code_line else "?"
        ok = http_code == "200"
        log(f"  NV inference: {'OK' if ok else 'FAIL'} (HTTP {http_code})")
        return ok
    except Exception as e:
        log(f"  NV inference: ERROR ({e})")
        return False

def save_status(top5, reachable, all_reachable=False, inference_ok=None):
    """Save status JSON."""
    status = {
        "timestamp": datetime.datetime.now().isoformat(),
        "top5": [{"key": i+1, "port": NV_K_PORTS[i],
                  "node": top5[i][0] if i < len(top5) else "unavailable",
                  "delay_ms": top5[i][1] if i < len(top5) else 0}
                 for i in range(5)],
        "total_reachable": len(reachable),
        "nv_api_reachable": all_reachable,
        "nv_inference_ok": inference_ok,
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
    log(f"Status saved to {STATUS_FILE}")

def main():
    do_test = "--test" in sys.argv
    do_status = "--status" in sys.argv

    if do_status:
        show_status()
        return

    log("=== R36.3 NV Proxy Selector — latency-based top5 selection ===")

    # Step 1: Test all US nodes against NV API
    reachable = test_all_nv_latency()

    if len(reachable) < 5:
        log(f"WARNING: Only {len(reachable)} reachable nodes (need 5)")
        # Try 204 fallback test for unreachable nodes
        # (some nodes might be reachable via 204 but not NV API)
        # For now, just use what we have

    # Step 2: Pick top5 (lowest latency, unique nodes)
    top5 = reachable[:5]
    log(f"Top5 selection:")
    for i, (name, delay) in enumerate(top5):
        log(f"  K{i+1} ({NV_K_PORTS[i]}): {name} ({delay}ms)")

    # Step 3: Assign top5 to K1-K5
    assign_top5(top5)

    # Step 4: Show current assignments
    show_status()

    # Step 5: Validate reachability
    all_reachable = validate_nv_reachability()

    # Step 6: Optionally test inference
    inference_ok = None
    if do_test and all_reachable:
        inference_ok = test_nv_inference()
    elif do_test and not all_reachable:
        log("NV API unreachable, skipping inference test")

    # Step 7: Save status
    save_status(top5, reachable, all_reachable, inference_ok)

    log(f"Done. Reachable: {len(reachable)} nodes, top5 assigned to K1-K5")

if __name__ == "__main__":
    main()
