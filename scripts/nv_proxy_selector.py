#!/usr/bin/env python3
# ── R38.8: NV Proxy Selector (mihomo health-check data reader) ──
# Reads NV API latency data from mihomo provider API (health-check url = NV API),
# ranks by delay, assigns top5 lowest-latency alive nodes to K1-K5.
# Each key gets a different node → different exit IP → IP diversity guaranteed.
#
# R38.8 key change: NO self-testing. mihomo kernel does health-check every 180s
# against NV API /v1/models (0 quota). Script just reads the data + sorts + assigns.
# Execution time: <3s (vs 30-60s with self-testing)
#
# Logic:
#   1. Query mihomo API /providers/proxies/nv-us-provider
#   2. Read NV delay from extra["https://integrate.api.nvidia.com/v1/models"]
#   3. Filter: nv_alive=True + alive=True + delay > 0
#   4. Sort by delay, pick top5 unique nodes
#   5. Assign top5 to ♻️US-NV-K1~K5 via mihomo PUT API
#   6. Optionally validate with curl (--test)
#
# Usage:
#   nv_proxy_selector.sh           — Read mihomo data, assign top5 to K1-K5
#   nv_proxy_selector.sh --test    — Assign + validate with curl
#   nv_proxy_selector.sh --status  — Show current K1-K5 assignments only
#
# Cron recommended (script is fast now, no self-testing):
#   */3 * * * *  nv_proxy_selector.sh           (re-assign every 3 min)

import json, sys, os, datetime, urllib.request, urllib.parse, time

MIHOMO_API = "http://127.0.0.1:9090"
MIHOMO_SECRET = "set-your-secret"
NV_API_URL = "https://integrate.api.nvidia.com/v1/models"
NV_API_KEY = "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"
STATUS_FILE = "/tmp/nv_proxy_status.json"
LOG_FILE = "/tmp/nv_proxy_selector.log"
NV_K_GROUPS = ["♻️US-NV-K1", "♻️US-NV-K2", "♻️US-NV-K3", "♻️US-NV-K4", "♻️US-NV-K5"]
NV_K_PORTS = [7894, 7895, 7896, 7897, 7899]

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = "[{ts}] {msg}".format(ts=ts, msg=msg)
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def mihomo_request(path, method="GET", data=None):
    url = MIHOMO_API + path
    headers = {"Authorization": "Bearer " + MIHOMO_SECRET}
    if data:
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method=method)
    else:
        req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return {}
        log("mihomo API error: {code} {reason}".format(code=e.code, reason=e.reason))
        return None
    except Exception as e:
        log("mihomo API error: " + str(e))
        return None

def read_nv_latency_from_mihomo():
    """Read NV API latency data from mihomo provider API.

    mihomo health-check (url=NV_API_URL, interval=180s) continuously tests
    all nodes. Delay data is stored in extra[NV_API_URL].history[-1].delay.
    No self-testing needed — data is always fresh (within 180s).
    """
    data = mihomo_request("/providers/proxies/nv-us-provider")
    if not data:
        log("ERROR: Failed to read mihomo provider data")
        return []

    proxies = data.get("proxies", [])
    if not proxies:
        log("ERROR: nv-us-provider has no proxies (subscription not loaded)")
        return []

    reachable = []
    for p in proxies:
        name = p.get("name", "?")
        alive = p.get("alive", False)
        extra = p.get("extra", {})

        # Read NV API delay from extra
        nv_data = extra.get(NV_API_URL, {})
        nv_hist = nv_data.get("history", [])
        nv_delay = nv_hist[-1].get("delay", -1) if nv_hist else -1
        nv_alive = nv_data.get("alive", False)

        # Only include nodes that are alive to both mihomo AND NV API
        if alive and nv_alive and nv_delay > 0:
            reachable.append((name, nv_delay))

    reachable.sort(key=lambda x: x[1])

    log("Read {total} nodes from mihomo API, {alive} alive to NV API".format(
        total=len(proxies), alive=len(reachable)))
    for i, (name, delay) in enumerate(reachable[:10]):
        log("  #{rank}: {delay}ms — {name}".format(rank=i+1, delay=delay, name=name))

    return reachable

def assign_top5(top5):
    """Assign top5 nodes to K1-K5 via mihomo PUT API."""
    if len(top5) < 5:
        log("WARNING: Only {n} reachable nodes, need 5 for K1-K5".format(n=len(top5)))

    for i, (node_name, delay) in enumerate(top5[:5]):
        group = NV_K_GROUPS[i]
        encoded = urllib.parse.quote(group)
        port = NV_K_PORTS[i]
        result = mihomo_request("/proxies/" + encoded, method="PUT", data={"name": node_name})
        log("  K{k} ({port}): assigned {name} ({delay}ms)".format(
            k=i+1, port=port, name=node_name, delay=delay))

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
        log("  K{k} ({port}): {now} (pool={pool})".format(
            k=i+1, port=NV_K_PORTS[i], now=now, pool=pool))

def validate_nv_reachability():
    """Quick check: test each K port against NV API via curl."""
    import subprocess
    log("=== Validating NV API reachability per port ===")
    all_ok = True
    for port in NV_K_PORTS:
        try:
            proc = subprocess.run(
                ["curl", "-s", "-x", "http://127.0.0.1:{port}".format(port=port),
                 "https://integrate.api.nvidia.com/v1/models", "--max-time", "10",
                 "-o", "/dev/null", "-w", "%{http_code} %{time_total}"],
                capture_output=True, text=True, timeout=15
            )
            parts = proc.stdout.strip().split()
            http_code = parts[0] if parts else "000"
            time_s = parts[1] if len(parts) > 1 else "?"
            ok = http_code == "200"
            status = "OK" if ok else "FAIL"
            log("  Port {port}: {status} (HTTP {code}, {time}s)".format(
                port=port, status=status, code=http_code, time=time_s))
            if not ok:
                all_ok = False
        except Exception as e:
            log("  Port {port}: ERROR ({e})".format(port=port, e=e))
            all_ok = False
    return all_ok

def save_status(top5, reachable, all_reachable=False):
    """Save status JSON."""
    status = {
        "timestamp": datetime.datetime.now().isoformat(),
        "top5": [{"key": i+1, "port": NV_K_PORTS[i],
                  "node": top5[i][0] if i < len(top5) else "unavailable",
                  "delay_ms": top5[i][1] if i < len(top5) else 0}
                 for i in range(5)],
        "total_reachable": len(reachable),
        "nv_api_reachable": all_reachable,
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
    log("Status saved to " + STATUS_FILE)

def main():
    do_test = "--test" in sys.argv
    do_status = "--status" in sys.argv

    if do_status:
        show_status()
        return

    log("=== R38.8 NV Proxy Selector — mihomo health-check data reader ===")

    # Step 1: Read NV latency data from mihomo API (no self-testing)
    reachable = read_nv_latency_from_mihomo()

    if len(reachable) < 5:
        log("WARNING: Only {n} reachable nodes (need 5)".format(n=len(reachable)))

    # Step 2: Pick top5 (lowest latency, unique nodes)
    top5 = reachable[:5]
    log("Top5 selection:")
    for i, (name, delay) in enumerate(top5):
        log("  K{k} ({port}): {name} ({delay}ms)".format(
            k=i+1, port=NV_K_PORTS[i], name=name, delay=delay))

    # Step 3: Assign top5 to K1-K5
    assign_top5(top5)

    # Step 4: Show current assignments
    show_status()

    # Step 5: Optionally validate with curl
    all_reachable = False
    if do_test:
        all_reachable = validate_nv_reachability()

    # Step 6: Save status
    save_status(top5, reachable, all_reachable)

    log("Done. Reachable: {n} nodes, top5 assigned to K1-K5".format(n=len(reachable)))

if __name__ == "__main__":
    main()
