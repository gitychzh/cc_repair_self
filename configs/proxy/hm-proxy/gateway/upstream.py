#!/usr/bin/env python3
"""Upstream request executor for Hermes NV proxy — R38.

R38: Routes through LiteLLM containers (41101-41105) instead of direct
HTTPS CONNECT tunnel to NV API. Each LiteLLM container has its own per-key
mihomo proxy (7894-7899) configured at container level via HTTPS_PROXY env var.

Chain: hm40006 → LiteLLM 41101-41105 → mihomo per-key proxy → NV API
hm40006 does: model name mapping + 5-key sequential RR + MSG-FIX + throttle
LiteLLM does: NV API call (with drop_params for unsupported params)

5-key sequential round-robin with persistent counter.
On 429/500/502: cycle to next LiteLLM container.
All 5 containers exhausted → return error to Hermes.
"""
import json
import http.client
import socket
import time
import urllib.parse

from .config import (
    HM_LITELLM_URLS, HM_NUM_KEYS, HM_LITELLM_KEY,
    NV_MODEL_IDS, DEFAULT_NV_MODEL, detect_nv_model, litellm_model_name,
    UPSTREAM_TIMEOUT,
    _next_hm_nv_key,
    throttle_outbound,
)
from .logger import _log


class UpstreamResult:
    """Result from LiteLLM upstream request execution."""
    def __init__(self):
        self.success = False
        # Success fields
        self.resp = None
        self.conn = None
        self.nv_model_label = ""
        self.nv_key_idx = 0
        self.is_stream = False
        self.key_cycle_attempts = []
        self.upstream_type = "nv_litellm"
        # Error fields
        self.all_keys_exhausted = False
        self.all_429 = False
        self.elapsed_ms = 0
        self.final_error_json = None
        self.final_resp_status = 0


def _make_litellm_conn(litellm_url, timeout=UPSTREAM_TIMEOUT):
    """Create HTTPConnection to LiteLLM container.

    LiteLLM containers are on the cc-net Docker network, accessible by
    container name (e.g. ms_nv_hm_41101) on port 4000.
    """
    parsed = urllib.parse.urlparse(litellm_url)
    host = parsed.hostname
    port = parsed.port or 4000
    path_prefix = parsed.path.rstrip("/")

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn, path_prefix


def execute_litellm_request(handler, oai_body, mapped_model, request_id, metrics, t_start):
    """Execute NV request via LiteLLM containers with 5-key sequential RR.

    R38: Sequential cycling k1→k2→k3→k4→k5→k1...
    Counter is persistent (rr_counter.json "hm_nv" key).
    Each key routes to its own LiteLLM container with per-key mihomo proxy.

    On 429/500/502: cycle to next LiteLLM container.
    All 5 containers exhausted → return error to Hermes.
    """
    result = UpstreamResult()
    result.is_stream = oai_body.get("stream", False)

    # Build LiteLLM request body — replace model name
    litellm_body = dict(oai_body)

    # Get starting key from persistent counter
    start_key_idx = _next_hm_nv_key()
    key_cycle_attempts = []

    _log("HM-RR", f"start_key=k{start_key_idx+1} model={mapped_model} "
                  f"(HM_NUM_KEYS={HM_NUM_KEYS}, sequential round-robin via LiteLLM)")

    for attempt_idx in range(HM_NUM_KEYS):
        key_idx = (start_key_idx + attempt_idx) % HM_NUM_KEYS
        litellm_url = HM_LITELLM_URLS[key_idx]
        nv_model_label = f"nvk{key_idx+1}"

        # Set model name for this LiteLLM container: e.g. nvkimi_k1
        litellm_body["model"] = litellm_model_name(mapped_model, key_idx)

        _log("HM-LITELLM", f"attempt {attempt_idx+1}/{HM_NUM_KEYS}: k{key_idx+1} "
                            f"→ {litellm_url} model={litellm_body['model']}")

        litellm_data = json.dumps(litellm_body).encode("utf-8")
        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {HM_LITELLM_KEY}",
            "Content-Length": str(len(litellm_data)),
        }

        try:
            conn, path_prefix = _make_litellm_conn(litellm_url, UPSTREAM_TIMEOUT)
            litellm_path = path_prefix.rstrip("/") + "/chat/completions"
            throttle_outbound()
            conn.request("POST", litellm_path, body=litellm_data, headers=headers_out)
            resp = conn.getresponse()

            if resp.status >= 400:
                error_body = resp.read()
                try:
                    error_json = json.loads(error_body)
                except Exception:
                    error_json = {"error": error_body.decode("utf-8", errors="replace")}
                conn.close()
                err_str = json.dumps(error_json)

                # Cycling errors: 429/500/502 → next LiteLLM container
                should_cycle = resp.status in (429, 500, 502)
                if should_cycle:
                    cycle_reason = "429_nv_rate_limit" if resp.status == 429 else \
                                   "500_nv_error" if resp.status == 500 else "502_nv_error"
                    key_cycle_attempts.append({
                        "nv_key_idx": key_idx,
                        "litellm_model": nv_model_label,
                        "error_body": err_str[:500],
                        "error_type": cycle_reason,
                        "upstream_type": "nv_litellm",
                    })
                    _log("HM-CYCLE", f"LiteLLM k{key_idx+1} ({nv_model_label}) → {resp.status} "
                                      f"({cycle_reason}), cycling to next")
                    continue

                # Non-cycling error → report
                result.final_error_json = error_json
                result.final_resp_status = resp.status
                result.key_cycle_attempts = key_cycle_attempts
                result.elapsed_ms = int((time.time() - t_start) * 1000)
                return result

            # ─── LiteLLM Success ───
            result.success = True
            result.resp = resp
            result.conn = conn
            result.nv_model_label = nv_model_label
            result.nv_key_idx = key_idx
            result.key_cycle_attempts = key_cycle_attempts
            metrics["upstream_type"] = "nv_litellm"
            metrics["nv_key_idx"] = key_idx
            metrics["litellm_model"] = nv_model_label
            if key_cycle_attempts:
                metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                metrics["key_cycle_details"] = key_cycle_attempts
                _log("HM-SUCCESS", f"LiteLLM k{key_idx+1} succeeded after {len(key_cycle_attempts)} cycle attempts")
            return result

        except socket.timeout as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-TIMEOUT", f"LiteLLM k{key_idx+1} timeout after {elapsed_ms}ms")
            key_cycle_attempts.append({
                "nv_key_idx": key_idx,
                "litellm_model": nv_model_label,
                "error_type": "LiteLLMTimeout",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv_litellm",
            })
            continue

        except (ConnectionRefusedError, http.client.RemoteDisconnected) as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-CONN", f"LiteLLM k{key_idx+1} connection error: {e}")
            key_cycle_attempts.append({
                "nv_key_idx": key_idx,
                "litellm_model": nv_model_label,
                "error_type": f"LiteLLM{type(e).__name__}",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv_litellm",
            })
            continue

        except Exception as e:
            error_class = type(e).__name__
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-ERR", f"LiteLLM k{key_idx+1} {error_class}: {e}")
            key_cycle_attempts.append({
                "nv_key_idx": key_idx,
                "litellm_model": nv_model_label,
                "error": str(e)[:200],
                "error_type": f"LiteLLM{error_class}",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv_litellm",
            })
            continue

    # All LiteLLM containers exhausted
    result.all_keys_exhausted = True
    result.all_429 = all(
        a.get("error_type") == "429_nv_rate_limit"
        for a in key_cycle_attempts
    )
    result.key_cycle_attempts = key_cycle_attempts
    result.elapsed_ms = int((time.time() - t_start) * 1000)
    _log("HM-ALL-FAIL", f"All {HM_NUM_KEYS} LiteLLM containers exhausted, elapsed={result.elapsed_ms}ms")
    return result
