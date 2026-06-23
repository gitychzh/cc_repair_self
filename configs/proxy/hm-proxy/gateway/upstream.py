#!/usr/bin/env python3
"""Upstream request executor for Hermes NV proxy — R37.

NV-only routing: 5 keys in sequential round-robin (k1→k2→k3→k4→k5→k1...).
Persistent counter: position saved to rr_counter.json after every increment.
No MS (ModelScope) routing — this proxy is purely NV API direct tunnel.

Per-key proxy URL (NV_PROXY_URL_MAP): each NV key routes through a different
mihomo port (7894-7899) for IP diversity on the US exit.

R36.2 fix: sock.settimeout(NV_TIMEOUT) after conn.request() for read timeout.
NV unsupported params strip: thinking_budget/reasoning_effort/stream_options/thinking.
"""
import json
import http.client
import socket
import ssl
import time
import datetime
import urllib.parse

from .config import (
    NV_BASEURL, NV_NUM_KEYS, NV_KEYS, NV_PROXY_URL, NV_ENABLED, NV_MODEL_IDS,
    NV_TIMEOUT, NV_PROXY_URL_MAP, DEFAULT_NV_MODEL,
    _next_hm_nv_key,
    throttle_outbound, MIN_OUTBOUND_INTERVAL_S,
)
from .logger import _log, _log_metrics, _log_error_detail


class UpstreamResult:
    """Result from NV upstream request execution."""
    def __init__(self):
        self.success = False
        # Success fields
        self.resp = None
        self.conn = None
        self.nv_model_label = ""
        self.nv_key_idx = 0
        self.is_stream = False
        self.key_cycle_attempts = []
        self.upstream_type = "nv"
        # Error fields
        self.all_keys_exhausted = False
        self.all_429 = False
        self.all_non_quota_429 = False
        self.elapsed_ms = 0
        self.final_error_json = None
        self.final_resp_status = 0


def _make_nv_conn(nv_baseurl, nv_proxy_url=None, timeout=NV_TIMEOUT):
    """Create HTTPConnection for NVIDIA API call, optionally through HTTPS proxy tunnel.

    Uses http.client.HTTPSConnection with CONNECT tunneling through mihomo proxy.
    R36.2: timeout only covers TCP+SSL+CONNECT phase. Read phase (getresponse())
    needs sock.settimeout() after conn.request().
    """
    parsed = urllib.parse.urlparse(nv_baseurl)
    host = parsed.hostname
    port = parsed.port or 443

    if nv_proxy_url:
        proxy_parsed = urllib.parse.urlparse(nv_proxy_url)
        proxy_host = proxy_parsed.hostname
        proxy_port = proxy_parsed.port or 7894
        conn = http.client.HTTPSConnection(
            proxy_host, proxy_port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        conn.set_tunnel(host, port)
        return conn, parsed.path
    else:
        conn = http.client.HTTPSConnection(
            host, port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        return conn, parsed.path


def _strip_nv_unsupported_params(oai_body):
    """Strip parameters that NVIDIA API doesn't support.

    NVIDIA does NOT support:
      - thinking_budget → 400 "Unsupported parameter(s)"
      - reasoning_effort → 400
      - stream_options → not standard, may error
      - thinking → not standard

    Returns a copy with these params removed.
    """
    body = dict(oai_body)
    for key in ("thinking_budget", "reasoning_effort", "stream_options", "thinking"):
        if key in body:
            del body[key]
    return body


def execute_nv_request(handler, oai_body, mapped_model, request_id, metrics, t_start):
    """Execute NV-only upstream request with 5-key sequential round-robin.

    R37: Sequential cycling k1→k2→k3→k4→k5→k1...
    Counter is persistent (rr_counter.json "hm_nv" key).
    Position continues from where it left off (not from 0 on restart).

    On 429/500/502: cycle to next NV key (no MS fallback).
    All 5 keys exhausted → return error to Hermes.
    """
    result = UpstreamResult()
    result.is_stream = oai_body.get("stream", False)

    # Determine NV model ID from frontend model name
    nv_model_id = NV_MODEL_IDS.get(mapped_model, DEFAULT_NV_MODEL)

    # Strip NV unsupported params
    nv_body = _strip_nv_unsupported_params(oai_body)
    nv_body["model"] = nv_model_id

    # Get starting key from persistent counter
    start_key_idx = _next_hm_nv_key()
    key_cycle_attempts = []

    _log("HM-RR", f"start_key=k{start_key_idx+1} model={nv_model_id} "
                  f"(NV_NUM_KEYS={NV_NUM_KEYS}, sequential round-robin)")

    for attempt_idx in range(NV_NUM_KEYS):
        nv_key_idx = (start_key_idx + attempt_idx) % NV_NUM_KEYS
        nv_key = NV_KEYS[nv_key_idx]
        nv_model_label = f"nvk{nv_key_idx+1}"

        # Per-key proxy URL for IP diversity
        proxy_url = NV_PROXY_URL_MAP.get(str(nv_key_idx), NV_PROXY_URL)

        _log("HM-NV", f"NV attempt {attempt_idx+1}/{NV_NUM_KEYS}: k{nv_key_idx+1} "
                       f"→ model={nv_model_id} proxy={proxy_url}")

        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {nv_key}",
            "Content-Length": str(len(json.dumps(nv_body).encode("utf-8"))),
        }
        nv_data = json.dumps(nv_body).encode("utf-8")

        try:
            conn, path_prefix = _make_nv_conn(NV_BASEURL, proxy_url, NV_TIMEOUT)
            nv_path = path_prefix.rstrip("/") + "/chat/completions"
            throttle_outbound()
            conn.request("POST", nv_path, body=nv_data, headers=headers_out)

            # R36.2 critical fix: sock.settimeout() after conn.request()
            # HTTPSConnection.timeout only covers connect phase (TCP+SSL+CONNECT).
            # getresponse() (read phase) needs sock.settimeout() for read timeout.
            if conn.sock:
                conn.sock.settimeout(NV_TIMEOUT)

            resp = conn.getresponse()

            if resp.status >= 400:
                error_body = resp.read()
                try:
                    error_json = json.loads(error_body)
                except Exception:
                    error_json = {"error": error_body.decode("utf-8", errors="replace")}
                conn.close()
                err_str = json.dumps(error_json)

                # Cycling errors: 429/500/502 → next key
                should_cycle = resp.status in (429, 500, 502)
                if should_cycle:
                    cycle_reason = "429_nv_rate_limit" if resp.status == 429 else \
                                   "500_nv_error" if resp.status == 500 else "502_nv_error"
                    key_cycle_attempts.append({
                        "nv_key_idx": nv_key_idx,
                        "litellm_model": nv_model_label,
                        "error_body": err_str[:500],
                        "error_type": cycle_reason,
                        "upstream_type": "nv",
                    })
                    _log("HM-CYCLE", f"NV k{nv_key_idx+1} ({nv_model_label}) → {resp.status} "
                                      f"({cycle_reason}), cycling to next key")
                    continue

                # 400 Unsupported parameter → strip and retry once
                if resp.status == 400 and "Unsupported parameter" in err_str:
                    _log("HM-STRIP", f"NV 400 Unsupported parameter → stripping and retrying")
                    nv_body_retry = _strip_nv_unsupported_params(nv_body)
                    nv_data_retry = json.dumps(nv_body_retry).encode("utf-8")
                    headers_retry = dict(headers_out)
                    headers_retry["Content-Length"] = str(len(nv_data_retry))
                    try:
                        conn2, path_prefix2 = _make_nv_conn(NV_BASEURL, proxy_url, NV_TIMEOUT)
                        throttle_outbound()
                        conn2.request("POST", nv_path, body=nv_data_retry, headers=headers_retry)
                        if conn2.sock:
                            conn2.sock.settimeout(NV_TIMEOUT)
                        resp2 = conn2.getresponse()
                        if resp2.status < 400:
                            result.success = True
                            result.resp = resp2
                            result.conn = conn2
                            result.nv_model_label = nv_model_label
                            result.nv_key_idx = nv_key_idx
                            result.key_cycle_attempts = key_cycle_attempts
                            metrics["upstream_type"] = "nv"
                            metrics["nv_key_idx"] = nv_key_idx
                            metrics["litellm_model"] = nv_model_label
                            return result
                        # Strip retry also failed
                        error_body2 = resp2.read()
                        try:
                            error_json2 = json.loads(error_body2)
                        except Exception:
                            error_json2 = {"error": error_body2.decode("utf-8", errors="replace")}
                        conn2.close()
                        if resp2.status in (429, 500, 502):
                            key_cycle_attempts.append({
                                "nv_key_idx": nv_key_idx,
                                "litellm_model": nv_model_label,
                                "error_body": json.dumps(error_json2)[:500],
                                "error_type": f"{resp2.status}_nv_after_strip",
                                "upstream_type": "nv",
                            })
                            continue
                        # Non-cycling after strip → report
                        result.final_error_json = error_json2
                        result.final_resp_status = resp2.status
                        result.key_cycle_attempts = key_cycle_attempts
                        result.elapsed_ms = int((time.time() - t_start) * 1000)
                        return result
                    except Exception as e2:
                        _log("HM-ERR", f"NV strip retry connection error: {e2}")
                        continue

                # Non-cycling, non-retryable error → report
                result.final_error_json = error_json
                result.final_resp_status = resp.status
                result.key_cycle_attempts = key_cycle_attempts
                result.elapsed_ms = int((time.time() - t_start) * 1000)
                return result

            # ─── NV Success ───
            result.success = True
            result.resp = resp
            result.conn = conn
            result.nv_model_label = nv_model_label
            result.nv_key_idx = nv_key_idx
            result.key_cycle_attempts = key_cycle_attempts
            metrics["upstream_type"] = "nv"
            metrics["nv_key_idx"] = nv_key_idx
            metrics["litellm_model"] = nv_model_label
            if key_cycle_attempts:
                metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                metrics["key_cycle_details"] = key_cycle_attempts
                _log("HM-SUCCESS", f"NV k{nv_key_idx+1} succeeded after {len(key_cycle_attempts)} cycle attempts")
            return result

        except socket.timeout as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-TIMEOUT", f"NV k{nv_key_idx+1} timeout after {elapsed_ms}ms")
            key_cycle_attempts.append({
                "nv_key_idx": nv_key_idx,
                "litellm_model": nv_model_label,
                "error_type": "NVSocketTimeout",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv",
            })
            continue

        except Exception as e:
            error_class = type(e).__name__
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-ERR", f"NV k{nv_key_idx+1} {error_class}: {e}")
            key_cycle_attempts.append({
                "nv_key_idx": nv_key_idx,
                "litellm_model": nv_model_label,
                "error": str(e)[:200],
                "error_type": f"NV{error_class}",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv",
            })
            continue

    # All NV keys exhausted
    result.all_keys_exhausted = True
    result.all_429 = all(
        a.get("error_type") in ("429_nv_rate_limit", "429_nv_rate_limit_variant_fallback")
        for a in key_cycle_attempts
    )
    result.key_cycle_attempts = key_cycle_attempts
    result.elapsed_ms = int((time.time() - t_start) * 1000)
    _log("HM-ALL-FAIL", f"All {NV_NUM_KEYS} NV keys exhausted, elapsed={result.elapsed_ms}ms")
    return result
