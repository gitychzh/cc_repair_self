#!/usr/bin/env python3
"""Upstream request executor for Hermes NV proxy — R38.10.

R38.10: deepseek-v4-pro bypasses DEGRADING integrate API → NVCF pexec orion (ACTIVE).
        kimi restored as primary. deepseek data collection experiment concluded.
        Mixed path: deepseek → NVCF pexec direct; kimi/glm5.1 → LiteLLM via integrate API.
R38.7: 3-tier fallback restored (glm5.1→kimi→deepseek).
       Data: nv_proxy_selector reselected nodes → deepseek 3/5 ports succeed now
       TIER_TIMEOUT_BUDGET_S 90→60s.
R38.8 NEW: Connection refused fast-break + startup retry.
       1. Consecutive 2+ ConnectionRefusedError/gaierror within same tier →
          fast-break to next tier (skip cycling all 7 keys, saves 3-10s per tier).
       2. If all tiers fail with ONLY connection errors (not 429/timeout/empty200),
          wait 5s and retry once (handles LiteLLM restart transient).
       3. docker-compose hm40006 depends_on now uses condition: service_healthy.
R38.6 critical fixes preserved: sock.settimeout BEFORE getresponse, Connection:close.
R38.5: throttle_outbound() only on first key attempt (not during cycling).
R38.2→R38.4: Per-tier persistent RR counters, dual suffix convention.

Default tier: kimi_hm_nv (primary), deepseek_hm_nv (fallback 1, NVCF pexec), glm5.1_hm_nv (fallback 2).
If all 5 keys fail → fallback to next tier.
If all tiers also all-fail → ABORT-NO-FALLBACK.

Each tier continues from its current key position (not from k1).
Empty 200 detection: choices=null, content=null, empty choices list.

Chain (kimi/glm5.1): hm40006 → LiteLLM 41101-41105 → mihomo per-key proxy → integrate API
Chain (deepseek): hm40006 → NVCF pexec (orion ACTIVE) → per-key SOCKS5 proxy → mihomo → NV API
"""
import json
import http.client
import socket
import ssl
import time
import urllib.parse

import socks  # PySocks — SOCKS5 proxy support for NVCF pexec

from .config import (
    HM_LITELLM_URLS, HM_NUM_KEYS, HM_LITELLM_KEY,
    HM_NV_KEYS, HM_NV_NUM_KEYS, HM_NV_PROXY_URLS,
    NV_MODEL_IDS, NV_MODEL_TIERS, DEFAULT_NV_MODEL, detect_nv_model,
    get_tier_index, litellm_model_name,
    NVCF_PEXEC_MODELS,
    UPSTREAM_TIMEOUT, TIER_TIMEOUT_BUDGET_S,
    _next_hm_nv_key,
    throttle_outbound,
    is_key_cooling, mark_key_cooling, reset_key429_count,
)
from .logger import _log, _log_metrics, _log_error_detail


class UpstreamResult:
    """Result from LiteLLM upstream request execution."""
    def __init__(self):
        self.success = False
        # Success fields
        self.resp = None
        self.conn = None
        self.tier_model = ""
        self.nv_key_idx = 0
        self.nv_model_label = ""
        self.is_stream = False
        self.key_cycle_attempts = []
        self.upstream_type = "nv_litellm"
        self.tier_attempts = []  # R38.2: per-tier attempt summary
        self.fallback_tiers_used = []  # R38.2: which tiers were tried
        # Error fields
        self.all_keys_exhausted = False
        self.all_429 = False
        self.empty_200 = False
        self.elapsed_ms = 0
        self.final_error_json = None
        self.final_resp_status = 0


def _make_litellm_conn(litellm_url, timeout=UPSTREAM_TIMEOUT):
    """Create HTTPConnection to LiteLLM container."""
    parsed = urllib.parse.urlparse(litellm_url)
    host = parsed.hostname
    port = parsed.port or 4000
    path_prefix = parsed.path.rstrip("/")
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn, path_prefix


def _make_nvcf_proxy_conn(proxy_url, nvcf_host="api.nvcf.nvidia.com", timeout=UPSTREAM_TIMEOUT):
    """Create HTTPSConnection to NVCF API via per-key mihomo SOCKS5 proxy.

    R38.10: deepseek bypasses integrate API (DEGRADING) → NVCF pexec (ACTIVE orion).
    Uses SOCKS5 proxy through per-key mihomo port (7894-7899) — mihomo mixed ports
    support SOCKS5 but NOT HTTP CONNECT tunnel (400 Bad Request).

    Connection flow: create SOCKS5 socket → connect to nvcf_host:443
    via mihomo proxy → wrap with SSL → inject into HTTPSConnection.

    Args:
        proxy_url: e.g. "http://host.docker.internal:7894"
        nvcf_host: NVCF API hostname (default: api.nvcf.nvidia.com)
        timeout: connect timeout (read timeout set via sock.settimeout later)

    Returns: HTTPSConnection with SOCKS5-proxied SSL socket, ready for request()
    """
    parsed = urllib.parse.urlparse(proxy_url)
    proxy_host = parsed.hostname
    proxy_port = parsed.port or 7894

    # Create SOCKS5 socket, connect through mihomo proxy to NVCF host
    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5, proxy_host, proxy_port)
    s.settimeout(timeout)
    s.connect((nvcf_host, 443))

    # Wrap with SSL (HTTPS)
    ctx = ssl.create_default_context()
    ss = ctx.wrap_socket(s, server_hostname=nvcf_host)

    # Inject the already-connected SSL socket into HTTPSConnection
    conn = http.client.HTTPSConnection(nvcf_host, 443, timeout=timeout)
    conn.sock = ss
    return conn


def _check_empty_200(resp, key_idx, tier_model, is_stream):
    """Check if a 200 response is actually empty (no real content).

    NV API can return 200 with null choices, null content, or empty response.
    These are treated as failures and trigger key cycling or fallback.

    For streaming: don't read body (would break stream). Use Content-Length=0
    as a hint. Stream empty content will be caught in SSE parsing.
    For non-streaming: read body and check choices/content.

    Returns: True if empty 200, False if valid response.
    On valid non-stream: sets resp_body on the resp object for later use.
    """
    content_length_str = resp.getheader("Content-Length", "-1")
    transfer_encoding = resp.getheader("Transfer-Encoding", "")

    if is_stream:
        # Streaming: can't read body. Content-Length=0 is a strong signal.
        if content_length_str == "0":
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 Content-Length:0 (stream)")
            return True
        # Otherwise trust the response — stream will naturally end if empty
        return False

    # Non-streaming: read and inspect body
    resp_body = resp.read()
    # Store body on resp for later retrieval (avoid double-read)
    resp._hm_cached_body = resp_body

    if not resp_body or len(resp_body) == 0:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 empty body (0 bytes)")
        return True

    try:
        oai_resp = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        # Can't parse as JSON — could be garbage, treat as empty
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 unparseable body ({len(resp_body)}b)")
        return True

    choices = oai_resp.get("choices")
    # choices is None/null → empty
    if choices is None:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices=null")
        return True
    # choices is empty list → empty
    if isinstance(choices, list) and len(choices) == 0:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices=[] (empty)")
        return True
    # choices[0] is null → empty
    if isinstance(choices, list) and choices[0] is None:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices[0]=null")
        return True
    # choices[0].message.content is null → empty
    if isinstance(choices, list) and len(choices) > 0:
        msg = choices[0].get("message")
        if msg is None:
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 message=null")
            return True
        content = msg.get("content")
        if content is None:
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 content=null")
            return True

    # Valid response with real content
    return False


def _try_tier_keys(oai_body, tier_model, request_id, metrics, t_start,
                   is_stream, prior_cycle_attempts):
    """Try all 5 keys within one tier, starting from current RR position.

    On 429/500/502: cycle to next key within same tier.
    On empty 200: cycle to next key within same tier.
    On other error: report immediately (no cycling).

    R38.5: 429 cooldown restored. Keys that recently got 429 are skipped
    (cooldown duration configurable via KEY_COOLDOWN_S env var).
    Attempt range doubled (HM_NUM_KEYS * 2) to allow cooldown recovery.

    Returns: UpstreamResult
      - success=True: valid response found
      - success=False, empty_200=True: all keys returned empty 200
      - success=False, all_429=True: all keys returned 429
      - success=False: mixed failures within tier
    """
    result = UpstreamResult()
    result.is_stream = is_stream
    result.tier_model = tier_model
    key_cycle_attempts = list(prior_cycle_attempts)

    nv_model_id = NV_MODEL_IDS[tier_model]
    _log("HM-TIER", f"Starting tier={tier_model} model={nv_model_id} "
                    f"(position from rr_counter)")

    # Get starting key from per-tier persistent counter
    start_key_idx = _next_hm_nv_key(tier_model)

    # R38.7: Tier timeout budget (60s) — stop trying keys in this tier if cumulative
    # elapsed time exceeds TIER_TIMEOUT_BUDGET_S. Prevents stacking of timeouts
    # (5 keys × 45s each = 225s worst case). Budget of 60s allows 1 timeout(45s)
    # + 1 retry window(15s); if 1 key already timed out, the tier is likely broken.
    tier_budget_start = time.time()

    # R38.8: Connection refused fast-break counter.
    # If 2+ consecutive keys hit ConnectionRefusedError or gaierror, the
    # LiteLLM layer is unreachable — break to next tier immediately instead
    # of cycling all 7 keys (each attempt adds 1-3s delay with zero value).
    consecutive_conn_err = 0
    CONN_ERR_FAST_BREAK = 2

    # R38.10: Determine upstream path for this tier
    # deepseek → NVCF pexec direct (bypasses DEGRADING integrate API)
    # kimi/glm5.1 → LiteLLM via integrate API (routes to ACTIVE functions)
    use_nvcf_pexec = tier_model in NVCF_PEXEC_MODELS
    nvcf_config = NVCF_PEXEC_MODELS.get(tier_model, {}) if use_nvcf_pexec else None

    # R38.7: Attempt range = HM_NUM_KEYS + 2 (reduced from HM_NUM_KEYS * 2 = 10 → 7).
    # Rationale: with tier timeout budget(60s) + correct sock.settimeout(45), max worst
    # case per tier is now 7×45=315s → but budget caps at 60s. Extra 2 covers cooldown skips.
    for attempt_idx in range(HM_NUM_KEYS + 2):
        key_idx = (start_key_idx + attempt_idx) % HM_NUM_KEYS

        # R38.6: Tier timeout budget check — break if budget exceeded
        elapsed_in_tier = time.time() - tier_budget_start
        if elapsed_in_tier >= TIER_TIMEOUT_BUDGET_S:
            _log("HM-TIER-BUDGET", f"tier={tier_model} budget {TIER_TIMEOUT_BUDGET_S}s "
                                    f"exceeded after {elapsed_in_tier:.1f}s, "
                                    f"breaking to next tier")
            break

        # R38.5: Skip keys in 429 cooldown to avoid wasting requests
        if is_key_cooling(tier_model, key_idx):
            _log("HM-KEY", f"tier={tier_model} k{key_idx+1} is in cooldown (429), skipping")
            # After all keys checked once, if still no success and all are cooling, break to fallback
            if attempt_idx >= HM_NUM_KEYS and all(is_key_cooling(tier_model, k) for k in range(HM_NUM_KEYS)):
                _log("HM-TIER", f"tier={tier_model} all keys in cooldown, breaking to fallback")
                break
            continue

        # ─── Build request based on upstream path ───
        if use_nvcf_pexec:
            # R38.10: NVCF pexec path for deepseek (orion ACTIVE function)
            # Need NV API key and per-key proxy for direct access
            if HM_NV_NUM_KEYS == 0 or key_idx >= HM_NV_NUM_KEYS:
                _log("HM-PEXEC-ERR", f"tier={tier_model} k{key_idx+1} no NV key/proxy configured for NVCF pexec")
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "error_type": "nvcf_pexec_no_key",
                    "upstream_type": "nvcf_pexec",
                })
                continue

            nv_key = HM_NV_KEYS[key_idx]
            proxy_url = HM_NV_PROXY_URLS[key_idx] if key_idx < len(HM_NV_PROXY_URLS) else HM_NV_PROXY_URLS[0]
            function_id = nvcf_config["function_id"]
            nvcf_host = nvcf_config["nvcf_base_url"]
            nvcf_path = f"{nvcf_config['nvcf_path_prefix']}/{function_id}"

            # NVCF pexec body: keep model field (NVCF accepts both with/without)
            pexec_body = dict(oai_body)
            pexec_body["model"] = NV_MODEL_IDS[tier_model]  # actual NV model ID, not LiteLLM label
            # Strip NV unsupported params (NVCF pexec is direct to NV function)
            for param in ("thinking_budget", "reasoning_effort"):
                pexec_body.pop(param, None)
            # stream_options is OK (NVCF supports it)

            pexec_data = json.dumps(pexec_body).encode("utf-8")

            _log("HM-KEY", f"tier={tier_model} attempt {attempt_idx+1}/{HM_NUM_KEYS + 2}: "
                           f"k{key_idx+1} → NVCF pexec {function_id[:12]}... via {proxy_url}")

            headers_out = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {nv_key}",
                "Content-Length": str(len(pexec_data)),
                "Connection": "close",
            }

            try:
                # R38.5: throttle before making connection (SOCKS5 connect is a real outbound)
                if attempt_idx == 0:
                    throttle_outbound()
                conn = _make_nvcf_proxy_conn(proxy_url, nvcf_host=nvcf_host, timeout=UPSTREAM_TIMEOUT)
                conn.request("POST", nvcf_path, body=pexec_data, headers=headers_out)
                # R38.6 CRITICAL FIX: sock.settimeout() BEFORE getresponse()
                if conn.sock:
                    conn.sock.settimeout(UPSTREAM_TIMEOUT)
                resp = conn.getresponse()

                if resp.status >= 400:
                    error_body = resp.read()
                    try:
                        error_json = json.loads(error_body)
                    except Exception:
                        error_json = {"error": error_body.decode("utf-8", errors="replace")}
                    conn.close()
                    err_str = json.dumps(error_json)

                    consecutive_conn_err = 0

                    should_cycle = resp.status in (429, 408, 500, 502)
                    if should_cycle:
                        cycle_reason = "429_nv_rate_limit" if resp.status == 429 else \
                                       "408_nvcf_timeout" if resp.status == 408 else \
                                       "500_nv_error" if resp.status == 500 else "502_nv_error"
                        key_cycle_attempts.append({
                            "tier": tier_model,
                            "nv_key_idx": key_idx,
                            "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                            "error_body": err_str[:500],
                            "error_type": cycle_reason,
                            "upstream_type": "nvcf_pexec",
                        })
                        if resp.status == 429:
                            mark_key_cooling(tier_model, key_idx)
                            _log("HM-COOLDOWN", f"tier={tier_model} k{key_idx+1} marked cooling after 429 (NVCF pexec)")
                        _log("HM-CYCLE", f"tier={tier_model} k{key_idx+1} (NVCF pexec) → "
                                         f"{resp.status} ({cycle_reason}), cycling to next key")
                        continue

                    # Non-cycling error → report
                    result.final_error_json = error_json
                    result.final_resp_status = resp.status
                    result.key_cycle_attempts = key_cycle_attempts
                    result.elapsed_ms = int((time.time() - t_start) * 1000)
                    return result

                # ─── 200 response — check for empty ───
                is_empty = _check_empty_200(resp, key_idx, tier_model, is_stream)

                if is_empty:
                    key_cycle_attempts.append({
                        "tier": tier_model,
                        "nv_key_idx": key_idx,
                        "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                        "error_type": "empty_200",
                        "upstream_type": "nvcf_pexec",
                    })
                    _log("HM-EMPTY-CYCLE", f"tier={tier_model} k{key_idx+1} empty 200 (NVCF pexec), cycling")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue

                # ─── Valid success response ───
                consecutive_conn_err = 0
                result.success = True
                result.resp = resp
                result.conn = conn
                result.tier_model = tier_model
                result.nv_key_idx = key_idx
                result.nv_model_label = f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}"
                result.key_cycle_attempts = key_cycle_attempts
                result.fallback_tiers_used = [tier_model]
                result.upstream_type = "nvcf_pexec"
                reset_key429_count(tier_model, key_idx)
                metrics["upstream_type"] = "nvcf_pexec"
                metrics["tier_model"] = tier_model
                metrics["nv_key_idx"] = key_idx
                metrics["litellm_model"] = result.nv_model_label
                if key_cycle_attempts:
                    metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                    metrics["key_cycle_details"] = key_cycle_attempts
                    _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} (NVCF pexec) succeeded after "
                                        f"{len(key_cycle_attempts)} cycle attempts")
                else:
                    _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} (NVCF pexec) succeeded on first attempt")
                return result

            except socket.timeout as e:
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log("HM-TIMEOUT", f"tier={tier_model} k{key_idx+1} NVCF pexec timeout after {elapsed_ms}ms")
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                    "error_type": "NVCFPexecTimeout",
                    "elapsed_ms": elapsed_ms,
                    "upstream_type": "nvcf_pexec",
                })
                continue

            except (ConnectionRefusedError, http.client.RemoteDisconnected) as e:
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log("HM-CONN", f"tier={tier_model} k{key_idx+1} NVCF pexec connection error: {e}")
                consecutive_conn_err += 1
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                    "error_type": f"NVCFPexec{type(e).__name__}",
                    "elapsed_ms": elapsed_ms,
                    "upstream_type": "nvcf_pexec",
                })
                if consecutive_conn_err >= CONN_ERR_FAST_BREAK:
                    _log("HM-CONN-BREAK", f"tier={tier_model} {consecutive_conn_err} consecutive "
                                           f"connection errors → fast-break (NVCF pexec)")
                    break
                continue

            except Exception as e:
                error_class = type(e).__name__
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log("HM-ERR", f"tier={tier_model} k{key_idx+1} NVCF pexec {error_class}: {e}")
                if "gaierror" in error_class.lower() or "socket" in error_class.lower():
                    consecutive_conn_err += 1
                    if consecutive_conn_err >= CONN_ERR_FAST_BREAK:
                        _log("HM-CONN-BREAK", f"tier={tier_model} {consecutive_conn_err} consecutive "
                                               f"DNS/socket errors → fast-break (NVCF pexec)")
                        key_cycle_attempts.append({
                            "tier": tier_model,
                            "nv_key_idx": key_idx,
                            "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                            "error": str(e)[:200],
                            "error_type": f"NVCFPexec{error_class}",
                            "elapsed_ms": elapsed_ms,
                            "upstream_type": "nvcf_pexec",
                        })
                        break
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                    "error": str(e)[:200],
                    "error_type": f"NVCFPexec{error_class}",
                    "elapsed_ms": elapsed_ms,
                    "upstream_type": "nvcf_pexec",
                })
                continue

        else:
            # ─── LiteLLM path (kimi/glm5.1: integrate API routes to ACTIVE functions) ───
            litellm_url = HM_LITELLM_URLS[key_idx]
            model_label = litellm_model_name(tier_model, key_idx)

            # Build LiteLLM request body
            litellm_body = dict(oai_body)
            litellm_body["model"] = model_label

            _log("HM-KEY", f"tier={tier_model} attempt {attempt_idx+1}/{HM_NUM_KEYS + 2}: "
                           f"k{key_idx+1} → {litellm_url} model={model_label}")

            litellm_data = json.dumps(litellm_body).encode("utf-8")
            headers_out = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {HM_LITELLM_KEY}",
                "Content-Length": str(len(litellm_data)),
                "Connection": "close",  # R38.6: prevent connection reuse → BrokenPipe errors
            }

            try:
                conn, path_prefix = _make_litellm_conn(litellm_url, UPSTREAM_TIMEOUT)
                litellm_path = path_prefix.rstrip("/") + "/chat/completions"
                # R38.5: throttle only on first key attempt of a request, not during key cycling.
                if attempt_idx == 0:
                    throttle_outbound()
                conn.request("POST", litellm_path, body=litellm_data, headers=headers_out)
                # R38.6 CRITICAL FIX: sock.settimeout() BEFORE getresponse()
                if conn.sock:
                    conn.sock.settimeout(UPSTREAM_TIMEOUT)
                resp = conn.getresponse()

                if resp.status >= 400:
                    error_body = resp.read()
                    try:
                        error_json = json.loads(error_body)
                    except Exception:
                        error_json = {"error": error_body.decode("utf-8", errors="replace")}
                    conn.close()
                    err_str = json.dumps(error_json)

                    consecutive_conn_err = 0

                    should_cycle = resp.status in (429, 408, 500, 502)
                    if should_cycle:
                        cycle_reason = "429_nv_rate_limit" if resp.status == 429 else \
                                       "408_litellm_timeout" if resp.status == 408 else \
                                       "500_nv_error" if resp.status == 500 else "502_nv_error"
                        key_cycle_attempts.append({
                            "tier": tier_model,
                            "nv_key_idx": key_idx,
                            "litellm_model": model_label,
                            "error_body": err_str[:500],
                            "error_type": cycle_reason,
                            "upstream_type": "nv_litellm",
                        })
                        if resp.status == 429:
                            mark_key_cooling(tier_model, key_idx)
                            _log("HM-COOLDOWN", f"tier={tier_model} k{key_idx+1} marked cooling after 429")
                        _log("HM-CYCLE", f"tier={tier_model} k{key_idx+1} ({model_label}) → "
                                         f"{resp.status} ({cycle_reason}), cycling to next key")
                        continue

                    # Non-cycling error → report
                    result.final_error_json = error_json
                    result.final_resp_status = resp.status
                    result.key_cycle_attempts = key_cycle_attempts
                    result.elapsed_ms = int((time.time() - t_start) * 1000)
                    return result

                # ─── 200 response — check for empty ───
                is_empty = _check_empty_200(resp, key_idx, tier_model, is_stream)

                if is_empty:
                    key_cycle_attempts.append({
                        "tier": tier_model,
                        "nv_key_idx": key_idx,
                        "litellm_model": model_label,
                        "error_type": "empty_200",
                        "upstream_type": "nv_litellm",
                    })
                    _log("HM-EMPTY-CYCLE", f"tier={tier_model} k{key_idx+1} empty 200, cycling to next key")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue

                # ─── Valid success response ───
                consecutive_conn_err = 0  # R38.8: reset — we got a real response
                result.success = True
                result.resp = resp
                result.conn = conn
                result.tier_model = tier_model
                result.nv_key_idx = key_idx
                result.nv_model_label = model_label
                result.key_cycle_attempts = key_cycle_attempts
                result.fallback_tiers_used = [tier_model]
                # R38.5: Reset 429 count when key succeeds — cooldown exponential backoff resets
                reset_key429_count(tier_model, key_idx)
                metrics["upstream_type"] = "nv_litellm"
                metrics["tier_model"] = tier_model
                metrics["nv_key_idx"] = key_idx
                metrics["litellm_model"] = model_label
                if key_cycle_attempts:
                    metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                    metrics["key_cycle_details"] = key_cycle_attempts
                    _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} succeeded after "
                                        f"{len(key_cycle_attempts)} cycle attempts")
                else:
                    _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} succeeded on first attempt")
                return result

            except socket.timeout as e:
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log("HM-TIMEOUT", f"tier={tier_model} k{key_idx+1} timeout after {elapsed_ms}ms")
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "litellm_model": model_label,
                    "error_type": "LiteLLMTimeout",
                    "elapsed_ms": elapsed_ms,
                    "upstream_type": "nv_litellm",
                })
                continue

            except (ConnectionRefusedError, http.client.RemoteDisconnected) as e:
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log("HM-CONN", f"tier={tier_model} k{key_idx+1} connection error: {e}")
                consecutive_conn_err += 1  # R38.8: track consecutive connection errors
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "litellm_model": model_label,
                    "error_type": f"LiteLLM{type(e).__name__}",
                    "elapsed_ms": elapsed_ms,
                    "upstream_type": "nv_litellm",
                })
                # R38.8: Connection refused fast-break — if 2+ consecutive keys
                # can't even connect, the LiteLLM layer is unreachable. Break
                # to next tier instead of wasting time on remaining keys.
                if consecutive_conn_err >= CONN_ERR_FAST_BREAK:
                    _log("HM-CONN-BREAK", f"tier={tier_model} {consecutive_conn_err} consecutive "
                                           f"connection errors → fast-break to next tier "
                                           f"(elapsed={elapsed_ms}ms)")
                    break
                continue

            except Exception as e:
                error_class = type(e).__name__
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log("HM-ERR", f"tier={tier_model} k{key_idx+1} {error_class}: {e}")
                # R38.8: gaierror (DNS failure) also counts as connection error for fast-break
                if "gaierror" in error_class.lower() or "socket" in error_class.lower():
                    consecutive_conn_err += 1
                    if consecutive_conn_err >= CONN_ERR_FAST_BREAK:
                        _log("HM-CONN-BREAK", f"tier={tier_model} {consecutive_conn_err} consecutive "
                                               f"DNS/socket errors → fast-break to next tier "
                                               f"(elapsed={elapsed_ms}ms)")
                        key_cycle_attempts.append({
                            "tier": tier_model,
                            "nv_key_idx": key_idx,
                            "litellm_model": model_label,
                            "error": str(e)[:200],
                            "error_type": f"LiteLLM{error_class}",
                            "elapsed_ms": elapsed_ms,
                            "upstream_type": "nv_litellm",
                        })
                        break
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "litellm_model": model_label,
                    "error": str(e)[:200],
                    "error_type": f"LiteLLM{error_class}",
                    "elapsed_ms": elapsed_ms,
                    "upstream_type": "nv_litellm",
                })
                continue

    # ─── All keys in this tier exhausted ───
    # Classify: all 429, all empty 200, or mixed
    tier_attempts = [a for a in key_cycle_attempts if a.get("tier") == tier_model]
    all_429 = all(a.get("error_type") == "429_nv_rate_limit" for a in tier_attempts)
    all_empty = all(a.get("error_type") == "empty_200" for a in tier_attempts)

    result.all_keys_exhausted = True
    result.all_429 = all_429
    result.empty_200 = all_empty
    result.key_cycle_attempts = key_cycle_attempts
    result.elapsed_ms = int((time.time() - t_start) * 1000)

    fail_summary = f"429={sum(1 for a in tier_attempts if a.get('error_type')=='429_nv_rate_limit')}, " \
                   f"empty200={sum(1 for a in tier_attempts if a.get('error_type')=='empty_200')}, " \
                   f"timeout={sum(1 for a in tier_attempts if 'Timeout' in a.get('error_type',''))}, " \
                   f"other={sum(1 for a in tier_attempts if a.get('error_type') not in ('429_nv_rate_limit','empty_200') and 'Timeout' not in a.get('error_type',''))}"
    _log("HM-TIER-FAIL", f"tier={tier_model} all {HM_NUM_KEYS} keys failed: {fail_summary}, "
                          f"elapsed={result.elapsed_ms}ms")

    # R38.5: When ALL keys in a tier hit 429, mark entire tier for global cooldown.
    # This prevents rapid re-cycling when the tier recovers but then immediately 429s again.
    if all_429:
        for k in range(HM_NUM_KEYS):
            mark_key_cooling(tier_model, k, duration_s=15)  # 15s global tier cooldown
        _log("HM-GLOBAL-COOLDOWN", f"tier={tier_model} all keys 429. Marking all keys cooling 15 seconds")

    # Log error detail for tier failure
    _log_error_detail({
        "request_id": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "error_subcategory": f"tier_{tier_model}_all_keys_failed",
        "tier_model": tier_model,
        "tier_attempts": tier_attempts,
        "all_429": all_429,
        "all_empty_200": all_empty,
        "elapsed_ms": result.elapsed_ms,
    })

    return result


def execute_litellm_request(handler, oai_body, mapped_model, request_id, metrics, t_start):
    """Execute NV request via LiteLLM with three-tier fallback (R38.8).

    R38.8: Tier chain: glm5.1_hm_nv → kimi_hm_nv → deepseek_hm_nv
    - mapped_model determines starting tier (default: glm5.1_hm_nv)
    - Each tier tries 5 keys with per-tier persistent RR counter
    - On tier all-fail: fallback to next tier (from current position)
    - All tiers fail: ABORT-NO-FALLBACK
    - R38.8: If all tiers fail with ONLY connection errors (not 429/timeout),
      wait 5s and retry once (handles LiteLLM restart transient).
    """
    # Determine starting tier
    start_tier_idx = get_tier_index(mapped_model)
    is_stream = oai_body.get("stream", False)

    _log("HM-REQ", f"mapped_model={mapped_model} start_tier={NV_MODEL_TIERS[start_tier_idx]} "
                   f"stream={is_stream} tier_chain={NV_MODEL_TIERS[start_tier_idx:]}")

    # R38.8: Allow one startup retry if all tiers fail with only connection errors
    for retry_idx in range(2):
        all_attempts = []
        all_tier_summaries = []
        fallback_tiers_used = []

        for tier_idx in range(start_tier_idx, len(NV_MODEL_TIERS)):
            tier_model = NV_MODEL_TIERS[tier_idx]
            is_first_tier = (tier_idx == start_tier_idx)

            # R38.5 Tier Skip: if ALL keys in this tier are in cooldown,
            # skip the entire tier immediately instead of wasting time
            all_cooling = all(is_key_cooling(tier_model, k) for k in range(HM_NUM_KEYS))
            if all_cooling:
                _log("HM-TIER-SKIP", f"tier={tier_model} all {HM_NUM_KEYS} keys in cooldown, "
                                      f"skipping entire tier → next tier")
                all_tier_summaries.append({
                    "tier": tier_model,
                    "all_429": True,
                    "all_empty_200": False,
                    "num_attempts": 0,
                    "elapsed_ms": 0,
                    "skipped": True,
                })
                if not is_first_tier:
                    _log("HM-FALLBACK", f"Tier {NV_MODEL_TIERS[tier_idx-1]} all-failed → "
                                        f"falling back to {tier_model} (continuing from current position)")
                continue

            if not is_first_tier:
                _log("HM-FALLBACK", f"Tier {NV_MODEL_TIERS[tier_idx-1]} all-failed → "
                                    f"falling back to {tier_model} (continuing from current position)")

            tier_result = _try_tier_keys(oai_body, tier_model, request_id, metrics, t_start,
                                         is_stream, all_attempts)

            if tier_result.success and not tier_result.empty_200:
                # ─── Success at this tier ───
                tier_result.fallback_tiers_used = [NV_MODEL_TIERS[i] for i in range(start_tier_idx, tier_idx + 1)]
                if not is_first_tier:
                    _log("HM-FALLBACK-SUCCESS", f"Success on fallback tier {tier_model} after "
                                                f"primary {NV_MODEL_TIERS[start_tier_idx]} failed "
                                                f"(tried tiers: {tier_result.fallback_tiers_used})")
                    metrics["fallback_from"] = NV_MODEL_TIERS[tier_idx - 1]
                    metrics["fallback_to"] = tier_model
                metrics["tier_model"] = tier_result.tier_model
                metrics["fallback_tiers_used"] = tier_result.fallback_tiers_used
                if retry_idx > 0:
                    _log("HM-STARTUP-RETRY-SUCCESS", f"Startup retry #{retry_idx} succeeded "
                                                    f"after initial connection failure")
                    metrics["startup_retry"] = retry_idx
                return tier_result

            # ─── Tier all-failed: record and try next ───
            tier_attempts = [a for a in tier_result.key_cycle_attempts
                             if a.get("tier") == tier_model or a not in all_attempts]
            all_tier_summaries.append({
                "tier": tier_model,
                "all_429": tier_result.all_429,
                "all_empty_200": tier_result.empty_200,
                "num_attempts": len(tier_attempts),
                "elapsed_ms": tier_result.elapsed_ms,
            })
            all_attempts = list(tier_result.key_cycle_attempts)

            # Close any remaining connections from failed tier
            if tier_result.conn:
                try:
                    tier_result.conn.close()
                except Exception:
                    pass

        # ─── All tiers exhausted ───
        _log("HM-ALL-TIERS-FAIL", f"All {len(NV_MODEL_TIERS)-start_tier_idx} tiers failed "
                                   f"(tiers tried: {NV_MODEL_TIERS[start_tier_idx:]}), "
                                   f"elapsed={int((time.time() - t_start) * 1000)}ms, ABORT-NO-FALLBACK "
                                   f"(R38.8: 3-tier + conn-fast-break + startup retry)")

        # Determine overall classification
        has_429 = any(s.get("all_429") for s in all_tier_summaries)
        has_empty = any(s.get("all_empty_200") for s in all_tier_summaries)

        # R38.8: Check if ALL failures were connection errors (not 429/timeout/empty200)
        # This indicates a LiteLLM startup transient — worth retrying after a short wait.
        all_conn_err = not has_429 and not has_empty and all(
            ("Conn" in a.get("error_type", "") or "gai" in a.get("error_type", "").lower() or
             "socket" in a.get("error_type", "").lower())
            for a in all_attempts if a.get("upstream_type") == "nv_litellm"
        ) and len(all_attempts) > 0

        if all_conn_err and retry_idx == 0:
            _log("HM-STARTUP-RETRY", f"All tiers failed with only connection errors "
                                     f"(LiteLLM likely restarting). Waiting 5s before retry...")
            time.sleep(5)
            continue  # Retry the entire request

        # No retry or retry also failed — return final result
        break

    # Build final result
    has_429 = any(s.get("all_429") for s in all_tier_summaries)
    has_empty = any(s.get("all_empty_200") for s in all_tier_summaries)

    final_result = UpstreamResult()
    final_result.success = False
    final_result.all_keys_exhausted = True
    final_result.all_429 = has_429 and not has_empty
    final_result.empty_200 = has_empty
    final_result.key_cycle_attempts = all_attempts
    final_result.tier_attempts = all_tier_summaries
    final_result.fallback_tiers_used = [NV_MODEL_TIERS[i] for i in range(start_tier_idx, len(NV_MODEL_TIERS))]
    final_result.elapsed_ms = int((time.time() - t_start) * 1000)
    final_result.final_resp_status = 429 if has_429 else 502

    # Log comprehensive error detail
    _log_error_detail({
        "request_id": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "error_subcategory": "all_tiers_failed",
        "start_tier": NV_MODEL_TIERS[start_tier_idx],
        "tiers_tried": NV_MODEL_TIERS[start_tier_idx:],
        "tier_summaries": all_tier_summaries,
        "total_attempts": len(all_attempts),
        "elapsed_ms": final_result.elapsed_ms,
        "startup_retry_attempted": retry_idx > 0,
    })

    _log_metrics({
        "request_id": request_id,
        "error_subcategory": "all_tiers_failed",
        "start_tier": NV_MODEL_TIERS[start_tier_idx],
        "tiers_tried": final_result.fallback_tiers_used,
        "total_cycle_attempts": len(all_attempts),
        "elapsed_ms": final_result.elapsed_ms,
        "startup_retry_attempted": retry_idx > 0,
    })

    return final_result
