#!/usr/bin/env python3
"""Upstream request executor for Hermes NV proxy — R38.12.

R38.12: ALL models use NVCF pexec direct path (SOCKS5 → ACTIVE functions).
        LiteLLM 41101-41105 removed from active routing.
        Single code path: all tiers → _make_nvcf_proxy_conn → SOCKS5 → NVCF pexec.
        Per-model strip_params declaration (glm5.1 strips thinking_budget).
R38.11: deepseek primary → glm5.1 fallback → kimi last-resort.
R38.10: deepseek bypasses DEGRADING integrate API → NVCF pexec orion (ACTIVE).
R38.8:  Connection refused fast-break + startup retry.
R38.6:  sock.settimeout BEFORE getresponse, Connection:close.

Default tier: deepseek_hm_nv (primary), glm5.1_hm_nv (fallback 1), kimi_hm_nv (last-resort).
If all 5 keys fail → fallback to next tier.
If all tiers also all-fail → ABORT-NO-FALLBACK.

Chain (ALL models): hm40006 → NVCF pexec (per-model ACTIVE function) → per-key SOCKS5 proxy → mihomo → NV API
"""
import json
import http.client
import socket
import ssl
import time
import urllib.parse

import socks  # PySocks — SOCKS5 proxy support for NVCF pexec

from .config import (
    HM_NV_KEYS, HM_NUM_KEYS, HM_NV_PROXY_URLS,
    NV_MODEL_IDS, NV_MODEL_TIERS, DEFAULT_NV_MODEL, detect_nv_model,
    get_tier_index,
    NVCF_PEXEC_MODELS, NVCF_BASE_URL,
    UPSTREAM_TIMEOUT, TIER_TIMEOUT_BUDGET_S,
    _next_hm_nv_key,
    throttle_outbound,
    is_key_cooling, mark_key_cooling, reset_key429_count,
)
from .logger import _log, _log_metrics, _log_error_detail


class UpstreamResult:
    """Result from NVCF pexec upstream request execution."""
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
        self.upstream_type = "nvcf_pexec"
        self.tier_attempts = []
        self.fallback_tiers_used = []
        # Error fields
        self.all_keys_exhausted = False
        self.all_429 = False
        self.empty_200 = False
        self.elapsed_ms = 0
        self.final_error_json = None
        self.final_resp_status = 0


def _make_nvcf_proxy_conn(proxy_url, nvcf_host, timeout=UPSTREAM_TIMEOUT):
    """Create HTTPSConnection to NVCF API via per-key mihomo SOCKS5 proxy.

    R38.12: ALL models use this function (no LiteLLM path).
    Connection flow: SOCKS5 socket → connect to nvcf_host:443 via mihomo
    → wrap with SSL → inject into HTTPSConnection.

    Args:
        proxy_url: e.g. "http://host.docker.internal:7894"
        nvcf_host: NVCF API hostname (from NVCF_BASE_URL config)
        timeout: connect timeout (read timeout set via sock.settimeout later)

    Returns: HTTPSConnection with SOCKS5-proxied SSL socket, ready for request()
    """
    parsed = urllib.parse.urlparse(proxy_url)
    proxy_host = parsed.hostname
    proxy_port = parsed.port or 7894

    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5, proxy_host, proxy_port)
    s.settimeout(timeout)
    s.connect((nvcf_host, 443))

    ctx = ssl.create_default_context()
    ss = ctx.wrap_socket(s, server_hostname=nvcf_host)

    conn = http.client.HTTPSConnection(nvcf_host, 443, timeout=timeout)
    conn.sock = ss
    return conn


def _build_pexec_body(oai_body, tier_model, nvcf_config):
    """Build NVCF pexec request body with per-model param stripping.

    R38.12: Each model declares which params NVCF pexec rejects via strip_params.
    - deepseek/kimi: strip_params=[] → all params pass through ✅
    - glm5.1: strip_params=["thinking_budget"] → strip thinking_budget (NVCF 400) ❌
      reasoning_effort is OK (tested 200 OK) → NOT stripped.

    Args:
        oai_body: original OpenAI-format request body from Hermes
        tier_model: internal NV model key (deepseek_hm_nv/kimi_hm_nv/glm5.1_hm_nv)
        nvcf_config: NVCF_PEXEC_MODELS[tier_model] dict

    Returns: request body dict, ready for json.dumps
    """
    pexec_body = dict(oai_body)
    pexec_body["model"] = NV_MODEL_IDS[tier_model]

    # Per-model param stripping (declaration in nvcf_config["strip_params"])
    strip_params = nvcf_config.get("strip_params", [])
    for param in strip_params:
        pexec_body.pop(param, None)

    return pexec_body


def _check_empty_200(resp, key_idx, tier_model, is_stream):
    """Check if a 200 response is actually empty (no real content).

    NV API can return 200 with null choices, null content, or empty response.
    These are treated as failures and trigger key cycling or fallback.

    Returns: True if empty 200, False if valid response.
    On valid non-stream: sets resp._hm_cached_body for later use.
    """
    content_length_str = resp.getheader("Content-Length", "-1")

    if is_stream:
        # Streaming: can't read body. Content-Length=0 is a strong signal.
        if content_length_str == "0":
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 Content-Length:0 (stream)")
            return True
        return False

    # Non-streaming: read and inspect body
    resp_body = resp.read()
    resp._hm_cached_body = resp_body

    if not resp_body or len(resp_body) == 0:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 empty body (0 bytes)")
        return True

    try:
        oai_resp = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 unparseable body ({len(resp_body)}b)")
        return True

    choices = oai_resp.get("choices")
    if choices is None:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices=null")
        return True
    if isinstance(choices, list) and len(choices) == 0:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices=[] (empty)")
        return True
    if isinstance(choices, list) and choices[0] is None:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices[0]=null")
        return True
    if isinstance(choices, list) and len(choices) > 0:
        msg = choices[0].get("message")
        if msg is None:
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 message=null")
            return True
        content = msg.get("content")
        if content is None:
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 content=null")
            return True

    return False


def _try_tier_keys(oai_body, tier_model, request_id, metrics, t_start,
                   is_stream, prior_cycle_attempts):
    """Try all 5 keys within one tier via NVCF pexec, starting from current RR position.

    R38.12: ALL models use NVCF pexec. No LiteLLM branch.
    On 429/500/502: cycle to next key within same tier.
    On empty 200: cycle to next key within same tier.
    On other error: report immediately (no cycling).
    Connection refused fast-break: 2+ consecutive → break to next tier.
    Tier timeout budget: stop if cumulative time exceeds budget.

    Returns: UpstreamResult
    """
    result = UpstreamResult()
    result.is_stream = is_stream
    result.tier_model = tier_model
    key_cycle_attempts = list(prior_cycle_attempts)

    nv_model_id = NV_MODEL_IDS[tier_model]
    nvcf_config = NVCF_PEXEC_MODELS[tier_model]
    nvcf_host = NVCF_BASE_URL
    function_id = nvcf_config["function_id"]
    nvcf_path = f"/v2/nvcf/pexec/functions/{function_id}"

    _log("HM-TIER", f"Starting tier={tier_model} model={nv_model_id} "
                    f"func={function_id[:12]}... (position from rr_counter)")

    # Build request body with per-model param stripping
    pexec_body = _build_pexec_body(oai_body, tier_model, nvcf_config)

    # Get starting key from per-tier persistent counter
    start_key_idx = _next_hm_nv_key(tier_model)

    tier_budget_start = time.time()
    consecutive_conn_err = 0
    CONN_ERR_FAST_BREAK = 2

    for attempt_idx in range(HM_NUM_KEYS + 2):
        key_idx = (start_key_idx + attempt_idx) % HM_NUM_KEYS
        t_attempt_start = time.time()  # R38.14: per-attempt start time for accurate logging

        # Tier timeout budget check (before each attempt)
        elapsed_in_tier = time.time() - tier_budget_start
        if elapsed_in_tier >= TIER_TIMEOUT_BUDGET_S:
            _log("HM-TIER-BUDGET", f"tier={tier_model} budget {TIER_TIMEOUT_BUDGET_S}s "
                                    f"exceeded after {elapsed_in_tier:.1f}s, breaking")
            break

        # R38.14: per-attempt timeout respects remaining budget
        # This prevents the bug where budget=60s but actual elapsed=~92s
        # (budget was only checked BEFORE attempts, not during in-progress requests)
        remaining_budget = TIER_TIMEOUT_BUDGET_S - elapsed_in_tier
        MIN_ATTEMPT_TIMEOUT = 10  # Don't attempt if less than 10s budget remains (doomed attempt)
        if remaining_budget < MIN_ATTEMPT_TIMEOUT:
            _log("HM-TIER-BUDGET", f"tier={tier_model} budget {TIER_TIMEOUT_BUDGET_S}s "
                                    f"remaining {remaining_budget:.1f}s < {MIN_ATTEMPT_TIMEOUT}s minimum, breaking")
            break
        per_attempt_timeout = min(UPSTREAM_TIMEOUT, remaining_budget)

        # Skip keys in 429 cooldown
        if is_key_cooling(tier_model, key_idx):
            _log("HM-KEY", f"tier={tier_model} k{key_idx+1} is in cooldown (429), skipping")
            if attempt_idx >= HM_NUM_KEYS and all(is_key_cooling(tier_model, k) for k in range(HM_NUM_KEYS)):
                _log("HM-TIER", f"tier={tier_model} all keys in cooldown, breaking")
                break
            continue

        # ─── NVCF pexec request ───
        if HM_NUM_KEYS == 0 or key_idx >= len(HM_NV_KEYS):
            _log("HM-PEXEC-ERR", f"tier={tier_model} k{key_idx+1} no NV key/proxy configured")
            key_cycle_attempts.append({
                "tier": tier_model,
                "nv_key_idx": key_idx,
                "error_type": "nvcf_pexec_no_key",
                "upstream_type": "nvcf_pexec",
            })
            continue

        nv_key = HM_NV_KEYS[key_idx]
        proxy_url = HM_NV_PROXY_URLS[key_idx] if key_idx < len(HM_NV_PROXY_URLS) else HM_NV_PROXY_URLS[0]

        # Build per-attempt request (model field already set in pexec_body)
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
            # Throttle before making connection (SOCKS5 connect is a real outbound)
            if attempt_idx == 0:
                throttle_outbound()
            conn = _make_nvcf_proxy_conn(proxy_url, nvcf_host=nvcf_host, timeout=per_attempt_timeout)
            conn.request("POST", nvcf_path, body=pexec_data, headers=headers_out)
            # R38.6 CRITICAL FIX: sock.settimeout() BEFORE getresponse()
            # R38.14: use per_attempt_timeout (respects budget) instead of UPSTREAM_TIMEOUT
            if conn.sock:
                conn.sock.settimeout(per_attempt_timeout)
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
                        _log("HM-COOLDOWN", f"tier={tier_model} k{key_idx+1} marked cooling after 429")
                    _log("HM-CYCLE", f"tier={tier_model} k{key_idx+1} → "
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
                _log("HM-EMPTY-CYCLE", f"tier={tier_model} k{key_idx+1} empty 200, cycling")
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
                _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} succeeded after "
                                    f"{len(key_cycle_attempts)} cycle attempts")
            else:
                _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} succeeded on first attempt")
            return result

        except socket.timeout as e:
            # R38.14: use per-attempt elapsed, not request-level t_start
            attempt_elapsed_ms = int((time.time() - t_attempt_start) * 1000)
            total_elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-TIMEOUT", f"tier={tier_model} k{key_idx+1} NVCF pexec timeout: "
                               f"attempt={attempt_elapsed_ms}ms total={total_elapsed_ms}ms")
            key_cycle_attempts.append({
                "tier": tier_model,
                "nv_key_idx": key_idx,
                "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                "error_type": "NVCFPexecTimeout",
                "elapsed_ms": attempt_elapsed_ms,  # R38.14: per-attempt elapsed, not total
                "upstream_type": "nvcf_pexec",
            })
            continue

        except (ConnectionRefusedError, http.client.RemoteDisconnected) as e:
            attempt_elapsed_ms = int((time.time() - t_attempt_start) * 1000)  # R38.14
            _log("HM-CONN", f"tier={tier_model} k{key_idx+1} connection error: {e}")
            consecutive_conn_err += 1
            key_cycle_attempts.append({
                "tier": tier_model,
                "nv_key_idx": key_idx,
                "litellm_model": f"nvcf_{NV_MODEL_IDS[tier_model]}_k{key_idx+1}",
                "error_type": f"NVCFPexec{type(e).__name__}",
                "elapsed_ms": attempt_elapsed_ms,
                "upstream_type": "nvcf_pexec",
            })
            if consecutive_conn_err >= CONN_ERR_FAST_BREAK:
                _log("HM-CONN-BREAK", f"tier={tier_model} {consecutive_conn_err} consecutive "
                                       f"connection errors → fast-break")
                break
            continue

        except Exception as e:
            error_class = type(e).__name__
            elapsed_ms = int((time.time() - t_attempt_start) * 1000)  # R38.14: per-attempt
            _log("HM-ERR", f"tier={tier_model} k{key_idx+1} {error_class}: {e}")
            if "gaierror" in error_class.lower() or "socket" in error_class.lower():
                consecutive_conn_err += 1
                if consecutive_conn_err >= CONN_ERR_FAST_BREAK:
                    _log("HM-CONN-BREAK", f"tier={tier_model} {consecutive_conn_err} consecutive "
                                           f"DNS/socket errors → fast-break")
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

    # ─── All keys in this tier exhausted ───
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

    if all_429:
        for k in range(HM_NUM_KEYS):
            mark_key_cooling(tier_model, k, duration_s=15)
        _log("HM-GLOBAL-COOLDOWN", f"tier={tier_model} all keys 429. Marking all cooling 15s")

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


def execute_request(handler, oai_body, mapped_model, request_id, metrics, t_start):
    """Execute NVCF pexec request with three-tier fallback (R38.12).

    ALL models use NVCF pexec direct path. No LiteLLM routing.
    - mapped_model determines starting tier (default: deepseek_hm_nv)
    - Each tier tries 5 keys with per-tier persistent RR counter
    - On tier all-fail: fallback to next tier (from current position)
    - All tiers fail: ABORT-NO-FALLBACK
    - R38.8: If all tiers fail with ONLY connection errors, wait 5s and retry once.
    """
    start_tier_idx = get_tier_index(mapped_model)
    is_stream = oai_body.get("stream", False)

    _log("HM-REQ", f"mapped_model={mapped_model} start_tier={NV_MODEL_TIERS[start_tier_idx]} "
                   f"stream={is_stream} tier_chain={NV_MODEL_TIERS[start_tier_idx:]}")

    for retry_idx in range(2):
        all_attempts = []
        all_tier_summaries = []
        fallback_tiers_used = []

        for tier_idx in range(start_tier_idx, len(NV_MODEL_TIERS)):
            tier_model = NV_MODEL_TIERS[tier_idx]
            is_first_tier = (tier_idx == start_tier_idx)

            # Skip tier if all keys in cooldown
            all_cooling = all(is_key_cooling(tier_model, k) for k in range(HM_NUM_KEYS))
            if all_cooling:
                _log("HM-TIER-SKIP", f"tier={tier_model} all keys in cooldown, skipping")
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
                                        f"falling back to {tier_model}")
                continue

            if not is_first_tier:
                _log("HM-FALLBACK", f"Tier {NV_MODEL_TIERS[tier_idx-1]} all-failed → "
                                    f"falling back to {tier_model}")

            tier_result = _try_tier_keys(oai_body, tier_model, request_id, metrics, t_start,
                                         is_stream, all_attempts)

            if tier_result.success and not tier_result.empty_200:
                tier_result.fallback_tiers_used = [NV_MODEL_TIERS[i] for i in range(start_tier_idx, tier_idx + 1)]
                if not is_first_tier:
                    _log("HM-FALLBACK-SUCCESS", f"Success on fallback tier {tier_model} "
                                                f"after primary {NV_MODEL_TIERS[start_tier_idx]} failed")
                    metrics["fallback_from"] = NV_MODEL_TIERS[tier_idx - 1]
                    metrics["fallback_to"] = tier_model
                metrics["tier_model"] = tier_result.tier_model
                metrics["fallback_tiers_used"] = tier_result.fallback_tiers_used
                if retry_idx > 0:
                    _log("HM-STARTUP-RETRY-SUCCESS", f"Startup retry #{retry_idx} succeeded")
                    metrics["startup_retry"] = retry_idx
                return tier_result

            # Tier all-failed: record and try next
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

            if tier_result.conn:
                try:
                    tier_result.conn.close()
                except Exception:
                    pass

        # ─── All tiers exhausted ───
        _log("HM-ALL-TIERS-FAIL", f"All {len(NV_MODEL_TIERS)-start_tier_idx} tiers failed "
                                   f"(tiers: {NV_MODEL_TIERS[start_tier_idx:]}), "
                                   f"elapsed={int((time.time() - t_start) * 1000)}ms, ABORT-NO-FALLBACK")

        has_429 = any(s.get("all_429") for s in all_tier_summaries)
        has_empty = any(s.get("all_empty_200") for s in all_tier_summaries)

        # Check if ALL failures were connection errors only
        all_conn_err = not has_429 and not has_empty and all(
            ("Conn" in a.get("error_type", "") or "gai" in a.get("error_type", "").lower() or
             "socket" in a.get("error_type", "").lower())
            for a in all_attempts
        ) and len(all_attempts) > 0

        if all_conn_err and retry_idx == 0:
            _log("HM-STARTUP-RETRY", f"All tiers failed with only connection errors. Waiting 5s...")
            time.sleep(5)
            continue

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
