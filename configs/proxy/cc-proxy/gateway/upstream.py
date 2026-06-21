#!/usr/bin/env python3
"""Upstream request executor with v×k 2D round-robin + MS-NV interleaving + error cycling.

This module is the shared core for ALL agent types (CC/OpenClaw/OpenCode/Hermes/Codex).
It handles:
  - v×k 2D round-robin counter for even distribution across ModelScope keys
  - MS-NV interleaving (R33.2): alternate between MS and NV upstreams
  - NV direct API call (cc-proxy calls NVIDIA integrate API directly via HTTPS proxy)
  - 429/500/502 key cycling (same variant, shift key k→k+1) for MS
  - NV key cycling on 429/500/502 (5 NV keys, same pattern)
  - Timeout cycling, connection error cycling
  - All-keys-exhausted classification (all_429 vs mixed failures)
  - Thinking_budget fix retry (400 InvalidParameter → adjust params → retry same key)
  - Resilience retry (401/403 AuthenticationError → retry once)
  - NV thinking_budget/reasoning_effort strip (NVIDIA doesn't support these params)

R29: Removed LiteLLM fallback (ms_uni41002). Single LiteLLM container (ms_uni41001).
R33.2: NV direct API — no NV LiteLLM containers. cc-proxy calls NVIDIA API directly.

UpstreamResult is returned to handlers, which format the response per agent type:
  - CC (_cc): Anthropic format conversion
  - OpenClaw/OpenCode/Hermes (_ol/_oc/_hm): OpenAI format passthrough
  - Codex (_cx): Responses API format conversion (via codex module)
"""
import json
import re
import http.client
import socket
import ssl
import time
import datetime
import urllib.parse

from .config import (
    LITELLM_KEY, PROXY_TIMEOUT, UPSTREAM_TIMEOUT, NUM_KEYS, NUM_VARIANTS, VARIANT_IDS,
    MODEL_UPSTREAMS, DEFAULT_UPSTREAM_MODEL, OUTPUT_TOKEN_MARGIN,
    NV_BASEURL, NV_NUM_KEYS, NV_KEYS, NV_PROXY_URL, NV_ENABLED, NV_MODEL_IDS,
    NV_TIMEOUT,
    MS_NV_TOTAL_SLOTS,
    _next_variant_key_pair,
    throttle_outbound, MIN_OUTBOUND_INTERVAL_S,
)
from .logger import _log, _log_metrics, _log_error_detail
from .error_mapping import is_quota_exhaustion


class UpstreamResult:
    """Unified result from upstream request execution.

    Handlers check result.success:
      - True: resp + conn are available for response processing (stream or non-stream)
      - False: all_keys_exhausted error info is available for error formatting

    The handler is responsible for:
      - Reading the response body and formatting it (Anthropic or OpenAI)
      - Closing the connection
      - Formatting error responses per agent type
    """
    def __init__(self):
        self.success = False
        # Success fields
        self.resp = None  # http.client.HTTPResponse
        self.conn = None  # http.client.HTTPConnection
        self.litellm_model = ""  # e.g. "glm5.1v3k5" or "nvk1"
        self.variant_idx = 0  # 0-based variant index
        self.key_idx = 0  # 0-based key index
        self.is_stream = False  # whether upstream was asked for streaming
        self.force_stream_for_nonstream = False  # whether we forced stream for non-stream
        self.key_cycle_attempts = []  # list of cycle attempt dicts
        self.upstream_type = "ms"  # "ms" or "nv" — which upstream was used
        # Error fields (only if not success)
        self.all_keys_exhausted = False
        self.all_429 = False
        self.all_non_quota_429 = False
        self.has_500 = False
        self.has_502 = False
        self.has_timeout = False
        self.has_conn_err = False
        self.error_subcategory = ""
        self.elapsed_ms = 0
        self.final_error_json = None  # last error JSON from upstream
        self.final_resp_status = 0  # last upstream HTTP status


def _make_nv_conn(nv_baseurl, nv_proxy_url=None, timeout=NV_TIMEOUT):
    """Create HTTPConnection for NVIDIA API call, optionally through HTTPS proxy.

    NVIDIA API (integrate.api.nvidia.com) requires US proxy from China.
    Uses http.client.HTTPSConnection with proxy tunneling (CONNECT method).

    Args:
        nv_baseurl: e.g. "https://integrate.api.nvidia.com/v1"
        nv_proxy_url: e.g. "http://host.docker.internal:7894"
        timeout: connection timeout in seconds
    """
    parsed = urllib.parse.urlparse(nv_baseurl)
    host = parsed.hostname
    port = parsed.port or 443

    if nv_proxy_url:
        # Connect through HTTPS proxy (CONNECT tunnel)
        proxy_parsed = urllib.parse.urlparse(nv_proxy_url)
        proxy_host = proxy_parsed.hostname
        proxy_port = proxy_parsed.port or 7894
        conn = http.client.HTTPSConnection(
            proxy_host, proxy_port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        # Set tunnel to NVIDIA host — all requests go through CONNECT tunnel
        conn.set_tunnel(host, port)
        return conn, parsed.path  # path = "/v1"
    else:
        # Direct connection (no proxy)
        conn = http.client.HTTPSConnection(
            host, port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        return conn, parsed.path


def _strip_nv_unsupported_params(oai_body):
    """Strip parameters that NVIDIA API doesn't support.

    NVIDIA does NOT support:
      - thinking_budget (returns 400 "Unsupported parameter(s)")
      - reasoning_effort (returns 400, but slow — best to strip)
      - stream_options (not standard OpenAI, NVIDIA ignores but may error)

    Returns a copy of oai_body with these params removed.
    """
    body = dict(oai_body)
    for key in ("thinking_budget", "reasoning_effort", "stream_options"):
        if key in body:
            del body[key]
    # Also strip from nested objects if present
    if "thinking" in body:
        del body["thinking"]
    return body


def execute_request(handler, oai_body, mapped_model, request_id, metrics, t_start):
    """Execute upstream request with full v×k 2D round-robin + MS-NV interleaving + error cycling.

    R33.2: When NV_ENABLED and model="glm5.1", requests alternate between MS and NV
    upstreams. Each request gets a slot assignment from _next_variant_key_pair():
      - MS slot → standard LiteLLM v×k cycling
      - NV slot → direct NVIDIA API call via HTTPS proxy

    On error (429/500/502), cycling applies within the same upstream type:
      - MS error → cycle through remaining MS keys in this variant
      - NV error → cycle through remaining NV keys
    If all keys in the initial upstream type fail, fall through to the other type:
      - MS all-fail → try all NV keys
      - NV all-fail → try all MS keys in current variant

    Args:
        handler: ProxyHandler instance (needed for _make_upstream_conn)
        oai_body: dict — OpenAI-format request body
        mapped_model: str — backend model name ("glm5.1")
        request_id: str — unique request ID for logging
        metrics: dict — metrics dict to update
        t_start: float — request start timestamp

    Returns:
        UpstreamResult — check result.success for outcome
    """
    result = UpstreamResult()

    # Select MS upstream URL
    upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
    upstream = MODEL_UPSTREAMS[upstream_key]
    litellm_url = upstream["chat_url"]
    metrics["upstream"] = upstream_key

    # Get initial slot assignment from round-robin
    start_pair = _next_variant_key_pair(mapped_model)
    start_variant_idx = start_pair[0]
    start_key_idx = start_pair[1]
    start_upstream_type = start_pair[2]  # "ms" or "nv"
    start_nv_key_idx = start_pair[3]  # NV key index (0-based, only if "nv")
    litellm_model_base = mapped_model
    key_cycle_attempts = []

    is_stream = oai_body.get("stream", False)
    result.is_stream = is_stream

    # Determine if this is a forced-stream-for-nonstream request
    force_stream = oai_body.get("stream", False) and not metrics.get("_original_stream", True)

    # ─── Determine primary and fallback upstream types ───
    # R33.2: If initial slot is NV, try NV first then MS as fallback.
    # If initial slot is MS, try MS first then NV as fallback.
    # For NV disabled: pure MS (unchanged behavior).
    if NV_ENABLED and mapped_model == "glm5.1":
        primary_type = start_upstream_type  # "ms" or "nv" from round-robin
        _log("MS-NV", f"slot={start_upstream_type} variant=v{start_variant_idx+1 if start_upstream_type=='ms' else 0} "
                      f"key=k{start_key_idx+1 if start_upstream_type=='ms' else start_nv_key_idx+1} "
                      f"(NV interleaving enabled, {MS_NV_TOTAL_SLOTS} total slots)")
    else:
        primary_type = "ms"
        _log("KEY-RR", f"MS-only for {mapped_model} (NV disabled or non-glm5.1)")

    # ─── Phase 1: Try primary upstream type ───
    if primary_type == "nv":
        # NV primary — try all NV keys first
        result = _try_nv_keys(handler, oai_body, mapped_model, request_id, metrics, t_start,
                              start_nv_key_idx, key_cycle_attempts, is_stream, force_stream)
        if result.success:
            return result
        # All NV keys failed → fall through to MS
        _log("NV-FALLTHROUGH", f"All {NV_NUM_KEYS} NV keys failed → trying MS fallback")
        nv_cycle_attempts = result.key_cycle_attempts
        result = _try_ms_keys(handler, oai_body, mapped_model, request_id, metrics, t_start,
                              start_variant_idx, start_key_idx, key_cycle_attempts + nv_cycle_attempts,
                              is_stream, force_stream, litellm_url, litellm_model_base)
        # Merge NV cycle attempts into final result
        if not result.success:
            result.key_cycle_attempts = nv_cycle_attempts + result.key_cycle_attempts
        return result

    else:
        # MS primary — try MS keys first (original behavior, with NV fallback if enabled)
        result = _try_ms_keys(handler, oai_body, mapped_model, request_id, metrics, t_start,
                              start_variant_idx, start_key_idx, key_cycle_attempts,
                              is_stream, force_stream, litellm_url, litellm_model_base)
        if result.success:
            return result
        if not result.all_keys_exhausted:
            return result  # Non-cycling error (400, 401, etc) — no fallback

        # All MS keys exhausted → try NV fallback if enabled
        if NV_ENABLED and mapped_model == "glm5.1":
            _log("MS-FALLTHROUGH", f"All MS keys 429 → trying NV fallback")
            ms_cycle_attempts = result.key_cycle_attempts
            result = _try_nv_keys(handler, oai_body, mapped_model, request_id, metrics, t_start,
                                  0, ms_cycle_attempts, is_stream, force_stream)
            # Merge MS cycle attempts into final result
            if not result.success:
                result.key_cycle_attempts = ms_cycle_attempts + result.key_cycle_attempts
            return result

        # MS all-fail, NV not available → return error
        return result


def _try_nv_keys(handler, oai_body, mapped_model, request_id, metrics, t_start,
                  start_nv_key_idx, prior_cycle_attempts, is_stream, force_stream):
    """Try all NV keys starting from start_nv_key_idx, cycling on 429/500/502.

    Returns UpstreamResult. If all NV keys fail, result.all_keys_exhausted=True.
    """
    result = UpstreamResult()
    result.is_stream = is_stream
    key_cycle_attempts = list(prior_cycle_attempts)
    nv_model_id = NV_MODEL_IDS.get(mapped_model, "z-ai/glm-5.1")

    # Strip NV unsupported params
    nv_body = _strip_nv_unsupported_params(oai_body)
    nv_body["model"] = nv_model_id

    for attempt_idx in range(NV_NUM_KEYS):
        nv_key_idx = (start_nv_key_idx + attempt_idx) % NV_NUM_KEYS
        nv_key = NV_KEYS[nv_key_idx]
        nv_model_label = f"nvk{nv_key_idx+1}"

        _log("NV-RR", f"NV attempt {attempt_idx+1}/{NV_NUM_KEYS}: k{nv_key_idx+1} → model={nv_model_id}")

        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {nv_key}",
            "Content-Length": str(len(json.dumps(nv_body).encode("utf-8"))),
        }
        nv_data = json.dumps(nv_body).encode("utf-8")

        try:
            conn, path_prefix = _make_nv_conn(NV_BASEURL, NV_PROXY_URL, NV_TIMEOUT)
            # NVIDIA API path: /v1/chat/completions
            nv_path = path_prefix.rstrip("/") + "/chat/completions"
            throttle_outbound()
            conn.request("POST", nv_path, body=nv_data, headers=headers_out)
            resp = conn.getresponse()

            if resp.status >= 400:
                error_body = resp.read()
                try:
                    error_json = json.loads(error_body)
                except Exception:
                    error_json = {"error": error_body.decode("utf-8", errors="replace")}
                conn.close()
                err_str = json.dumps(error_json)

                # Cycling errors for NV
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
                    _log("NV-CYCLE", f"NV k{nv_key_idx+1}/{NV_NUM_KEYS} ({nv_model_label}) → {resp.status} ({cycle_reason}), cycling to next NV key")
                    continue

                # NV-specific: 400 Unsupported parameter → strip params and retry
                if resp.status == 400 and "Unsupported parameter" in err_str:
                    _log("NV-STRIP", f"NV 400 Unsupported parameter → stripping unsupported params and retrying")
                    nv_body_retry = _strip_nv_unsupported_params(nv_body)
                    nv_data_retry = json.dumps(nv_body_retry).encode("utf-8")
                    headers_retry = dict(headers_out)
                    headers_retry["Content-Length"] = str(len(nv_data_retry))
                    try:
                        conn2, path_prefix2 = _make_nv_conn(NV_BASEURL, NV_PROXY_URL, NV_TIMEOUT)
                        throttle_outbound()
                        conn2.request("POST", nv_path, body=nv_data_retry, headers=headers_retry)
                        resp2 = conn2.getresponse()
                        if resp2.status < 400:
                            result.success = True
                            result.resp = resp2
                            result.conn = conn2
                            result.litellm_model = nv_model_label
                            result.key_idx = nv_key_idx
                            result.key_cycle_attempts = key_cycle_attempts
                            result.upstream_type = "nv"
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
                        # If it's a cycling error after strip, continue cycling
                        if resp2.status in (429, 500, 502):
                            key_cycle_attempts.append({
                                "nv_key_idx": nv_key_idx,
                                "litellm_model": nv_model_label,
                                "error_body": json.dumps(error_json2)[:500],
                                "error_type": f"{resp2.status}_nv_after_strip",
                                "upstream_type": "nv",
                            })
                            continue
                        # Non-cycling error after strip → report
                        result.success = False
                        result.all_keys_exhausted = False
                        result.final_error_json = error_json2
                        result.final_resp_status = resp2.status
                        result.key_cycle_attempts = key_cycle_attempts
                        result.elapsed_ms = int((time.time() - t_start) * 1000)
                        return result
                    except Exception as e2:
                        _log("NV-ERR", f"NV strip retry connection error: {e2}")
                        continue

                # Non-cycling, non-retryable NV error → report
                result.success = False
                result.all_keys_exhausted = False
                result.final_error_json = error_json
                result.final_resp_status = resp.status
                result.key_cycle_attempts = key_cycle_attempts
                result.elapsed_ms = int((time.time() - t_start) * 1000)
                return result

            # ─── NV Success ───
            result.success = True
            result.resp = resp
            result.conn = conn
            result.litellm_model = nv_model_label
            result.key_idx = nv_key_idx
            result.key_cycle_attempts = key_cycle_attempts
            result.upstream_type = "nv"
            metrics["upstream_type"] = "nv"
            metrics["nv_key_idx"] = nv_key_idx
            metrics["litellm_model"] = nv_model_label
            if key_cycle_attempts:
                metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                metrics["key_cycle_details"] = key_cycle_attempts
                _log("NV-SUCCESS", f"NV k{nv_key_idx+1} succeeded after {len(key_cycle_attempts)} cycle attempts")
            return result

        except socket.timeout as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("NV-TIMEOUT", f"NV k{nv_key_idx+1} socket timeout after {elapsed_ms}ms")
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
            _log("NV-ERR", f"NV k{nv_key_idx+1} {error_class}: {e}")
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
    result.success = False
    result.all_keys_exhausted = True
    result.all_429 = all(a.get("error_type") in ("429_nv_rate_limit", "429_nv_rate_limit_variant_fallback") for a in key_cycle_attempts if a.get("upstream_type") == "nv")
    result.key_cycle_attempts = key_cycle_attempts
    result.elapsed_ms = int((time.time() - t_start) * 1000)
    _log("NV-ALL-FAIL", f"All {NV_NUM_KEYS} NV keys exhausted, elapsed={result.elapsed_ms}ms")
    return result


def _try_ms_keys(handler, oai_body, mapped_model, request_id, metrics, t_start,
                  start_variant_idx, start_key_idx, prior_cycle_attempts,
                  is_stream, force_stream, litellm_url, litellm_model_base):
    """Try MS (ModelScope via LiteLLM) keys with v×k cycling and error handling.

    This is the original execute_request logic, now as a sub-function for R33.2 MS-NV interleaving.
    Returns UpstreamResult. If all MS keys fail, result.all_keys_exhausted=True.
    """
    result = UpstreamResult()
    result.is_stream = is_stream
    key_cycle_attempts = list(prior_cycle_attempts)

    for attempt_idx in range(NUM_KEYS):
        # 429 cycling: same variant, shift key (k→k+1)
        current_key_idx = (start_key_idx + attempt_idx) % NUM_KEYS
        litellm_model = f"{litellm_model_base}v{start_variant_idx+1}k{current_key_idx+1}"
        oai_body["model"] = litellm_model
        _log("KEY-RR", f"attempt {attempt_idx+1}/{NUM_KEYS}: v{start_variant_idx+1} k{current_key_idx+1} → model={litellm_model}")

        # Build upstream request
        auth_key = handler.headers.get("x-api-key") or handler.headers.get("X-Api-Key") or LITELLM_KEY
        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_key}",
            "Content-Length": str(len(json.dumps(oai_body).encode("utf-8"))),
        }
        oai_data = json.dumps(oai_body).encode("utf-8")
        parsed_upstream = urllib.parse.urlparse(litellm_url)

        try:
            conn = handler._make_upstream_conn(parsed_upstream)
            throttle_outbound()  # R31.9: enforce MIN_OUTBOUND_INTERVAL_S to smooth burst
            conn.request("POST", parsed_upstream.path, body=oai_data, headers=headers_out)
            resp = conn.getresponse()

            # Extract LiteLLM routing/quota headers
            for hdr_key, metrics_key in [
                ("x-litellm-model-id", "litellm_model_id"),
                ("x-litellm-response-duration-ms", "litellm_response_duration_ms"),
            ]:
                val = resp.getheader(hdr_key)
                if val:
                    metrics[metrics_key] = val

            # Extract ModelScope quota headers
            for hdr_key, metrics_key in [
                ("llm_provider-modelscope-ratelimit-model-requests-remaining", "ms_model_requests_remaining"),
                ("llm_provider-modelscope-ratelimit-requests-remaining", "ms_requests_remaining"),
            ]:
                val = resp.getheader(hdr_key)
                if val:
                    metrics[metrics_key] = int(val)

            if resp.status >= 400:
                error_body = resp.read()
                try:
                    error_json = json.loads(error_body)
                except Exception:
                    error_json = {"error": error_body.decode("utf-8", errors="replace")}
                conn.close()
                err_str = json.dumps(error_json)

                # ─── Cycling errors: 429/500/502 → next key (same variant) ───
                should_cycle_to_next_key = resp.status in (429, 500, 502)

                if should_cycle_to_next_key:
                    if resp.status == 429:
                        if is_quota_exhaustion(error_json):
                            cycle_reason = "429_quota_exhausted"
                        else:
                            cycle_reason = "429_rate_limit"
                    else:
                        cycle_reason = {
                            500: "500_internal_server_error",
                            502: "502_bad_gateway",
                        }.get(resp.status, "unknown")
                    key_cycle_attempts.append({
                        "variant_idx": start_variant_idx,
                        "key_idx": current_key_idx,
                        "litellm_model": litellm_model,
                        "error_body": err_str[:500],
                        "error_type": cycle_reason,
                    })
                    _log_error_detail({
                        "request_id": request_id,
                        "timestamp": datetime.datetime.now().isoformat(),
                        "error_subcategory": f"{cycle_reason}_key_cycle_attempt",
                        "upstream_status": resp.status,
                        "variant_idx": start_variant_idx,
                        "key_idx": current_key_idx,
                        "litellm_model": litellm_model,
                        "attempt_number": attempt_idx + 1,
                        "total_keys": NUM_KEYS,
                        "upstream_error_body_full": err_str[:3000],
                    })
                    _log("KEY-CYCLE", f"v{start_variant_idx+1} k{current_key_idx+1}/{NUM_KEYS} ({litellm_model}) → {resp.status} ({cycle_reason}), cycling to next key")
                    continue  # Try next key

                # ─── Non-cycling errors: resilience retry or report ───

                # Thinking_budget fix retry (400 InvalidParameter)
                should_fix_thinking_budget = (
                    resp.status == 400
                    and "InvalidParameter" in err_str
                    and "thinking_budget" in err_str
                    and "max_completion_tokens" in err_str
                    and metrics.get("_thinking_budget_retry_count", 0) < 1
                )

                # Resilience retry (401/403 AuthenticationError)
                should_resilience_retry = (
                    resp.status in (401, 403)
                    and "AuthenticationError" in err_str
                    and metrics.get("_resilience_retry_count", 0) < 1
                )

                if should_fix_thinking_budget:
                    metrics["_thinking_budget_retry_count"] = metrics.get("_thinking_budget_retry_count", 0) + 1
                    _tb_match = re.search(r'thinking_budget\s*\[(\d+)\]', err_str)
                    _mc_match = re.search(r'max_completion_tokens\s*\[(\d+)\]', err_str)
                    if _tb_match and _mc_match:
                        actual_tb = int(_tb_match.group(1))
                        actual_mc = int(_mc_match.group(1))
                        fixed_mc = actual_tb + OUTPUT_TOKEN_MARGIN
                        _log("THINKFIX", f"thinking_budget={actual_tb} > max_completion_tokens={actual_mc} → fixing to {fixed_mc}")
                        oai_body_fixed = json.loads(oai_data.decode("utf-8"))
                        oai_body_fixed["max_tokens"] = fixed_mc
                        oai_body_fixed["max_completion_tokens"] = fixed_mc
                        if "thinking_budget" not in oai_body_fixed:
                            oai_body_fixed["thinking_budget"] = actual_tb
                        fixed_data = json.dumps(oai_body_fixed).encode("utf-8")
                        fixed_headers = dict(headers_out)
                        fixed_headers["Content-Length"] = str(len(fixed_data))
                        _log_error_detail({
                            "request_id": request_id,
                            "timestamp": datetime.datetime.now().isoformat(),
                            "error_subcategory": "400_thinking_budget_fix_retry",
                            "upstream_status": resp.status,
                            "original_max_completion_tokens": actual_mc,
                            "original_thinking_budget": actual_tb,
                            "fixed_max_completion_tokens": fixed_mc,
                            "upstream_error_body_full": err_str[:1000],
                        })
                        try:
                            conn_fix = handler._make_upstream_conn(parsed_upstream)
                            throttle_outbound()  # R31.9: burst smoothing
                            conn_fix.request("POST", parsed_upstream.path, body=fixed_data, headers=fixed_headers)
                            resp_fix = conn_fix.getresponse()
                            if resp_fix.status < 400:
                                # Success after thinking_budget fix!
                                result.success = True
                                result.resp = resp_fix
                                result.conn = conn_fix
                                result.litellm_model = litellm_model
                                result.variant_idx = start_variant_idx
                                result.key_idx = current_key_idx
                                result.key_cycle_attempts = key_cycle_attempts
                                result.upstream_type = "ms"
                                metrics["key_idx"] = current_key_idx
                                metrics["variant_idx"] = start_variant_idx
                                metrics["litellm_model"] = litellm_model
                                metrics["thinking_budget_fix_success"] = True
                                if key_cycle_attempts:
                                    metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                                    metrics["key_cycle_details"] = key_cycle_attempts
                                    _log("KEY-CYCLE-SUCCESS", f"429 on {len(key_cycle_attempts)} key(s) before success on v{start_variant_idx+1} k{current_key_idx+1}")
                                return result
                            # Fix retry also failed
                            error_body_fix = resp_fix.read()
                            try:
                                error_json_fix = json.loads(error_body_fix)
                            except Exception:
                                error_json_fix = {"error": error_body_fix.decode("utf-8", errors="replace")}
                            conn_fix.close()
                            error_json = error_json_fix
                            _log("ERR", f"thinking_budget fix retry also failed: {resp_fix.status} {json.dumps(error_json_fix)[:200]}")
                        except Exception as e_fix:
                            _log("ERR", f"thinking_budget fix retry connection error: {e_fix}")

                if should_resilience_retry:
                    metrics["_resilience_retry_count"] = metrics.get("_resilience_retry_count", 0) + 1
                    _log("RESILIENCE", f"401/403 AuthError → retry #{metrics['_resilience_retry_count']}")
                    _log_error_detail({
                        "request_id": request_id,
                        "timestamp": datetime.datetime.now().isoformat(),
                        "error_subcategory": "401_resilience_retry_triggered",
                        "upstream_status": resp.status,
                        "upstream_error_body_full": error_body.decode("utf-8", errors="replace")[:1000],
                    })
                    try:
                        conn2 = handler._make_upstream_conn(parsed_upstream)
                        throttle_outbound()  # R31.9: burst smoothing
                        conn2.request("POST", parsed_upstream.path, body=oai_data, headers=headers_out)
                        resp2 = conn2.getresponse()
                        if resp2.status < 400:
                            # Success after resilience retry!
                            result.success = True
                            result.resp = resp2
                            result.conn = conn2
                            result.litellm_model = litellm_model
                            result.variant_idx = start_variant_idx
                            result.key_idx = current_key_idx
                            result.key_cycle_attempts = key_cycle_attempts
                            result.upstream_type = "ms"
                            metrics["key_idx"] = current_key_idx
                            metrics["variant_idx"] = start_variant_idx
                            metrics["litellm_model"] = litellm_model
                            metrics["resilience_retry_success"] = True
                            if key_cycle_attempts:
                                metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                                metrics["key_cycle_details"] = key_cycle_attempts
                                _log("KEY-CYCLE-SUCCESS", f"429 on {len(key_cycle_attempts)} key(s) before success on v{start_variant_idx+1} k{current_key_idx+1}")
                            return result
                        # Resilience retry also failed
                        error_body2 = resp2.read()
                        try:
                            error_json2 = json.loads(error_body2)
                        except Exception:
                            error_json2 = {"error": error_body2.decode("utf-8", errors="replace")}
                        conn2.close()
                        error_json = error_json2
                        _log("ERR", f"resilience retry also failed: {resp2.status} {json.dumps(error_json2)[:200]}")
                    except Exception as e2:
                        _log("ERR", f"resilience retry connection error: {e2}")

                # ─── Non-cycling, non-retryable error → report ───
                resp_status_final = resp.status
                _log("ERR", f"upstream {resp_status_final}: {json.dumps(error_json)[:200]}")
                _log_error_detail({
                    "request_id": request_id,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "error_subcategory": f"{resp_status_final}_upstream_error",
                    "upstream_status": resp_status_final,
                    "upstream_error_body_full": json.dumps(error_json)[:3000],
                })
                metrics["status"] = resp_status_final
                metrics["error_type"] = "UpstreamError"
                metrics["error_message"] = json.dumps(error_json)[:200]
                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                _log_metrics(metrics)

                result.success = False
                result.all_keys_exhausted = False
                result.final_error_json = error_json
                result.final_resp_status = resp_status_final
                result.elapsed_ms = int((time.time() - t_start) * 1000)
                result.key_cycle_attempts = key_cycle_attempts
                result.upstream_type = "ms"
                return result

            # ─── Success: resp.status < 400 ───
            result.success = True
            result.resp = resp
            result.conn = conn
            result.litellm_model = litellm_model
            result.variant_idx = start_variant_idx
            result.key_idx = current_key_idx
            result.key_cycle_attempts = key_cycle_attempts
            result.upstream_type = "ms"

            # Update metrics
            metrics["key_idx"] = current_key_idx
            metrics["variant_idx"] = start_variant_idx
            metrics["litellm_model"] = litellm_model
            if key_cycle_attempts:
                metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                metrics["key_cycle_details"] = key_cycle_attempts
                _log("KEY-CYCLE-SUCCESS", f"429 on {len(key_cycle_attempts)} key(s) before success on v{start_variant_idx+1} k{current_key_idx+1} ({litellm_model})")
            return result  # Success — done

        except socket.timeout as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            timeout_detail = {
                "request_id": request_id,
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": "upstream_socket_timeout",
                "variant_idx": start_variant_idx,
                "key_idx": current_key_idx,
                "litellm_model": litellm_model,
                "attempt_number": attempt_idx + 1,
                "total_keys": NUM_KEYS,
                "upstream_timeout_setting_ms": UPSTREAM_TIMEOUT * 1000,
                "elapsed_since_request_start_ms": elapsed_ms,
                "timeout_exceeded_by_ms": elapsed_ms - UPSTREAM_TIMEOUT * 1000 if elapsed_ms > UPSTREAM_TIMEOUT * 1000 else 0,
                "error_message": str(e)[:200],
            }
            _log_error_detail(timeout_detail)
            _log("TIMEOUT", f"v{start_variant_idx+1} k{current_key_idx+1}/{NUM_KEYS} ({litellm_model}) socket timeout "
                           f"after {elapsed_ms}ms (UPSTREAM_TIMEOUT={UPSTREAM_TIMEOUT}s), cycling to next key")
            key_cycle_attempts.append({
                "variant_idx": start_variant_idx,
                "key_idx": current_key_idx,
                "litellm_model": litellm_model,
                "error": str(e)[:200],
                "error_type": "SocketTimeout",
                "elapsed_ms": elapsed_ms,
                "upstream_timeout_ms": UPSTREAM_TIMEOUT * 1000,
                "timeout_exceeded_by_ms": elapsed_ms - UPSTREAM_TIMEOUT * 1000 if elapsed_ms > UPSTREAM_TIMEOUT * 1000 else 0,
            })
            continue  # Try next key

        except Exception as e:
            err_str = str(e)
            error_class = type(e).__name__
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("ERR", f"v{start_variant_idx+1} k{current_key_idx+1} ({litellm_model}) {error_class}: {e} (elapsed={elapsed_ms}ms)")
            key_cycle_attempts.append({
                "variant_idx": start_variant_idx,
                "key_idx": current_key_idx,
                "litellm_model": litellm_model,
                "error": err_str[:200],
                "error_type": error_class,
                "elapsed_ms": elapsed_ms,
            })
            _log_error_detail({
                "request_id": request_id,
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": f"upstream_{error_class}",
                "variant_idx": start_variant_idx,
                "key_idx": current_key_idx,
                "litellm_model": litellm_model,
                "attempt_number": attempt_idx + 1,
                "total_keys": NUM_KEYS,
                "elapsed_since_request_start_ms": elapsed_ms,
                "error_message": err_str[:500],
            })
            continue  # Try next key

    # ─── All keys exhausted in start variant ───
    # Every key in this variant failed. Classify the failure type.

    all_429 = all(a.get("error_type") in (None, "429", "429_rate_limit", "429_quota_exhausted") for a in key_cycle_attempts)
    has_500 = any(a.get("error_type") == "500_internal_server_error" for a in key_cycle_attempts)
    has_502 = any(a.get("error_type") == "502_bad_gateway" for a in key_cycle_attempts)
    has_timeout = any(a.get("error_type") == "SocketTimeout" for a in key_cycle_attempts)
    has_conn_err = any(a.get("error_type") in ("ConnectionRefusedError", "ConnectionError") for a in key_cycle_attempts)

    # ─── Variant Fallback (R23) ───
    # When all 7 keys in the start variant are 429, try 2 fallback variants
    # (1 key each). This uses different variant's independent RPM quota.
    variant_fallback_attempts = []
    variant_fallback_success = False

    if False and all_429 and not has_500 and not has_502 and not has_timeout and not has_conn_err:
        # R31.8: Variant fallback DISABLED.
        # Previously, when all 7 keys returned 429, the proxy retried 9 fallback
        # variants (each 1 key) — 16-17x request amplification. In a systemic
        # failure (e.g. a software bug causing every request to 429), this would
        # rapidly burn through account quota. Per user requirement: 7 keys all-429
        # must terminate immediately and return the error to CC. CC retries on its
        # own terms; the proxy no longer amplifies. `if False` keeps the fallback
        # code intact for future re-enablement while guaranteeing it never runs.
        num_variants = NUM_VARIANTS.get(mapped_model, 10)
        # If ALL 429s are non-quota (RPM/false-positive), brief delay before sweeping all variants
        all_non_quota_v429 = all(
            a.get("error_type") in (None, "429", "429_rate_limit")
            for a in key_cycle_attempts
        )
        if all_non_quota_v429:
            _log("VARIANT-FALLBACK-DELAY", f"All {NUM_KEYS} non-quota 429s — 2s delay before sweeping ALL variants")
            time.sleep(2)
        _log("VARIANT-FALLBACK-START", f"All {NUM_KEYS} keys 429 for v{start_variant_idx+1}, "
                                       f"trying ALL fallback variants for {mapped_model}")

        for fallback_v_offset in range(1, num_variants):
            fallback_v_idx = (start_variant_idx + fallback_v_offset) % num_variants
            # R31.8: rotate the physical key too, not just the variant. Previously
            # fixed to start_key_idx, so all 9 fallback attempts landed on the SAME
            # physical key (e.g. all k7) and, since ModelScope RPM is per-key, all
            # 9 retried the same burst-exhausted key. Rotating by offset spreads
            # the 9 attempts across distinct physical keys.
            fallback_k_idx = (start_key_idx + fallback_v_offset) % num_keys
            litellm_model_fb = f"{litellm_model_base}v{fallback_v_idx+1}k{fallback_k_idx+1}"
            oai_body_fb = dict(oai_body)  # Clone body for fallback
            oai_body_fb["model"] = litellm_model_fb

            _log("VARIANT-FALLBACK-TRY", f"Fallback variant #{fallback_v_offset}: "
                                         f"v{fallback_v_idx+1} k{fallback_k_idx+1} → model={litellm_model_fb}")

            auth_key_fb = handler.headers.get("x-api-key") or handler.headers.get("X-Api-Key") or LITELLM_KEY
            headers_fb = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth_key_fb}",
                "Content-Length": str(len(json.dumps(oai_body_fb).encode("utf-8"))),
            }
            oai_data_fb = json.dumps(oai_body_fb).encode("utf-8")
            parsed_upstream_fb = urllib.parse.urlparse(litellm_url)

            try:
                conn_fb = handler._make_upstream_conn(parsed_upstream_fb)
                throttle_outbound()  # R31.9: burst smoothing
                conn_fb.request("POST", parsed_upstream_fb.path, body=oai_data_fb, headers=headers_fb)
                resp_fb = conn_fb.getresponse()

                # Extract LiteLLM routing/quota headers
                for hdr_key, metrics_key in [
                    ("x-litellm-model-id", "litellm_model_id"),
                    ("x-litellm-response-duration-ms", "litellm_response_duration_ms"),
                ]:
                    val_fb = resp_fb.getheader(hdr_key)
                    if val_fb:
                        metrics[metrics_key] = val_fb

                # Extract ModelScope quota headers
                for hdr_key, metrics_key in [
                    ("llm_provider-modelscope-ratelimit-model-requests-remaining", "ms_model_requests_remaining"),
                    ("llm_provider-modelscope-ratelimit-requests-remaining", "ms_requests_remaining"),
                ]:
                    val_fb = resp_fb.getheader(hdr_key)
                    if val_fb:
                        metrics[metrics_key] = int(val_fb)

                if resp_fb.status >= 400:
                    error_body_fb = resp_fb.read()
                    try:
                        error_json_fb = json.loads(error_body_fb)
                    except Exception:
                        error_json_fb = {"error": error_body_fb.decode("utf-8", errors="replace")}
                    conn_fb.close()
                    err_str_fb = json.dumps(error_json_fb)

                    # Only 429 errors should try next fallback variant
                    if resp_fb.status == 429:
                        if is_quota_exhaustion(error_json_fb):
                            fb_error_type = "429_quota_exhausted_variant_fallback"
                        else:
                            fb_error_type = "429_rate_limit_variant_fallback"
                        variant_fallback_attempts.append({
                            "variant_idx": fallback_v_idx,
                            "key_idx": fallback_k_idx,
                            "litellm_model": litellm_model_fb,
                            "error_body": err_str_fb[:500],
                            "error_type": fb_error_type,
                        })
                        _log_error_detail({
                            "request_id": request_id,
                            "timestamp": datetime.datetime.now().isoformat(),
                            "error_subcategory": "429_variant_fallback_attempt",
                            "upstream_status": resp_fb.status,
                            "variant_idx": fallback_v_idx,
                            "key_idx": fallback_k_idx,
                            "litellm_model": litellm_model_fb,
                            "fallback_offset": fallback_v_offset,
                            "upstream_error_body_full": err_str_fb[:3000],
                        })
                        _log("VARIANT-FALLBACK-429", f"v{fallback_v_idx+1} k{fallback_k_idx+1} ({litellm_model_fb}) → "
                                                   f"429, trying next fallback variant")
                        continue  # Try next fallback variant
                    else:
                        # Non-429 error in fallback — stop and include in final classification
                        variant_fallback_attempts.append({
                            "variant_idx": fallback_v_idx,
                            "key_idx": fallback_k_idx,
                            "litellm_model": litellm_model_fb,
                            "error_body": err_str_fb[:500],
                            "error_type": f"{resp_fb.status}_variant_fallback_non429",
                        })
                        _log("VARIANT-FALLBACK-ERR", f"v{fallback_v_idx+1} k{fallback_k_idx+1} ({litellm_model_fb}) → "
                                                    f"{resp_fb.status} non-429 error, stopping fallback")
                        break  # Non-429 error: stop fallback attempts

                else:
                    # ─── Variant fallback SUCCESS ───
                    variant_fallback_success = True
                    metrics["variant_fallback"] = True
                    metrics["fallback_variant_idx"] = fallback_v_idx
                    metrics["fallback_key_idx"] = fallback_k_idx
                    metrics["litellm_model"] = litellm_model_fb
                    metrics["key_idx"] = fallback_k_idx
                    metrics["variant_idx"] = fallback_v_idx
                    metrics["variant_fallback_attempts"] = variant_fallback_attempts
                    metrics["variant_fallback_429s_before_success"] = len(variant_fallback_attempts)

                    _log("VARIANT-FALLBACK-SUCCESS", f"Success on fallback variant v{fallback_v_idx+1} "
                                                      f"k{fallback_k_idx+1} ({litellm_model_fb}) after "
                                                      f"{len(key_cycle_attempts)} key 429s + {len(variant_fallback_attempts)} variant 429s")

                    result.success = True
                    result.resp = resp_fb
                    result.conn = conn_fb
                    result.litellm_model = litellm_model_fb
                    result.variant_idx = fallback_v_idx
                    result.key_idx = fallback_k_idx
                    result.key_cycle_attempts = key_cycle_attempts
                    if variant_fallback_attempts:
                        metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts) + len(variant_fallback_attempts)
                        metrics["key_cycle_details"] = key_cycle_attempts + variant_fallback_attempts
                    return result

            except socket.timeout as e_fb:
                elapsed_ms_fb = int((time.time() - t_start) * 1000)
                variant_fallback_attempts.append({
                    "variant_idx": fallback_v_idx,
                    "key_idx": fallback_k_idx,
                    "litellm_model": litellm_model_fb,
                    "error_type": "SocketTimeout_variant_fallback",
                    "elapsed_ms": elapsed_ms_fb,
                })
                _log("VARIANT-FALLBACK-TIMEOUT", f"v{fallback_v_idx+1} k{fallback_k_idx+1} ({litellm_model_fb}) "
                                                  f"socket timeout after {elapsed_ms_fb}ms, trying next variant")
                continue  # Try next fallback variant

            except Exception as e_fb:
                err_str_fb = str(e_fb)
                error_class_fb = type(e_fb).__name__
                variant_fallback_attempts.append({
                    "variant_idx": fallback_v_idx,
                    "key_idx": fallback_k_idx,
                    "litellm_model": litellm_model_fb,
                    "error_type": f"{error_class_fb}_variant_fallback",
                    "error": err_str_fb[:200],
                })
                _log("VARIANT-FALLBACK-CONNERR", f"v{fallback_v_idx+1} k{fallback_k_idx+1} ({litellm_model_fb}) "
                                                  f"{error_class_fb}: {err_str_fb[:100]}, trying next variant")
                continue  # Try next fallback variant

        # All fallback variants also failed
        _log("VARIANT-FALLBACK-ALL-FAILED", f"All {len(variant_fallback_attempts)} fallback variant attempts also 429 "
                                            f"for {mapped_model}. Original: {len(key_cycle_attempts)} key 429s in v{start_variant_idx+1}, "
                                            f"Fallback: {[a['litellm_model'] for a in variant_fallback_attempts]}")

        # Merge fallback attempts into classification
        all_attempts = key_cycle_attempts + variant_fallback_attempts
        all_429 = all(a.get("error_type") in (None, "429", "429_rate_limit", "429_quota_exhausted", "429_rate_limit_variant_fallback", "429_quota_exhausted_variant_fallback") for a in all_attempts)
        has_500 = any(a.get("error_type") in ("500_internal_server_error", "500_variant_fallback_non429") for a in all_attempts)
        has_502 = any(a.get("error_type") in ("502_bad_gateway", "502_variant_fallback_non429") for a in all_attempts)
        has_timeout = any(a.get("error_type") in ("SocketTimeout", "SocketTimeout_variant_fallback") for a in all_attempts)
        has_conn_err = any(a.get("error_type") in ("ConnectionRefusedError", "ConnectionError", "ConnectionRefusedError_variant_fallback", "ConnectionError_variant_fallback") for a in all_attempts)
    else:
        # Non-429 mixed errors: no variant fallback attempted
        all_attempts = key_cycle_attempts

    elapsed_ms = int((time.time() - t_start) * 1000)

    # R31.8: hard termination — no variant fallback. Log clearly so the
    # 7-key-all-429-immediate-stop is observable in real traffic.
    if all_429 and not variant_fallback_attempts:
        _log("ABORT-NO-FALLBACK", f"7 keys all-429 for v{start_variant_idx+1} ({mapped_model}) — "
                                 f"terminating immediately (no variant fallback), returning error to client. "
                                 f"Keys: {[a['litellm_model'] for a in key_cycle_attempts]} elapsed={elapsed_ms}ms")

    all_non_quota_429 = False
    if all_429:
        all_non_quota_429 = all(
            a.get("error_type") in (None, "429", "429_rate_limit", "429_rate_limit_variant_fallback")
            for a in all_attempts
        )
        if all_non_quota_429:
            error_subcategory = "429_all_transient"
            _log("ALL-KEYS-TRANSIENT", f"All 429s non-quota (transient) for {mapped_model}. "
                                       f"Key cycles: {[a['litellm_model'] for a in key_cycle_attempts]} "
                                       f"Fallback: {[a['litellm_model'] for a in variant_fallback_attempts]}")
        else:
            error_subcategory = "429_all_keys_exhausted"
            _log("ALL-KEYS-429", f"All keys 429 for {mapped_model} (including variant fallbacks). "
                                 f"Key cycles: {[a['litellm_model'] for a in key_cycle_attempts]} "
                                 f"Fallback: {[a['litellm_model'] for a in variant_fallback_attempts]}")
    elif has_500 or has_502:
        error_subcategory = "all_keys_500_or_502"
        _log("ALL-KEYS-500/502", f"All keys failed for {mapped_model} (includes 500/502). "
                                 f"Attempts: {[a['litellm_model'] for a in all_attempts]} "
                                 f"elapsed={elapsed_ms}ms")
    elif has_timeout:
        error_subcategory = "all_keys_timeout_or_429"
        _log("ALL-KEYS-TIMEOUT", f"All keys failed for {mapped_model} (includes timeouts). "
                                 f"Attempts: {[a['litellm_model'] for a in all_attempts]} "
                                 f"elapsed={elapsed_ms}ms")
    else:
        error_subcategory = "all_keys_connection_or_429"
        _log("ALL-KEYS-CONNERR", f"All keys failed for {mapped_model} (includes connection errors). "
                                 f"Attempts: {[a['litellm_model'] for a in all_attempts]}")

    _log_error_detail({
        "request_id": request_id,
        "timestamp": datetime.datetime.now().isoformat(),
        "error_subcategory": error_subcategory,
        "model": mapped_model,
        "total_keys": NUM_KEYS,
        "key_cycle_attempts": key_cycle_attempts,
        "key_cycle_attempt_types": [a.get("error_type", "429_rate_limit") for a in key_cycle_attempts],
        "variant_fallback_attempts": variant_fallback_attempts,
        "variant_fallback_attempt_types": [a.get("error_type") for a in variant_fallback_attempts],
        "elapsed_since_request_start_ms": elapsed_ms,
        "upstream_timeout_setting_ms": UPSTREAM_TIMEOUT * 1000,
        "all_429": all_429,
        "has_500": has_500,
        "has_502": has_502,
        "has_timeout": has_timeout,
        "has_connection_error": has_conn_err,
        "upstream_error_body_full": json.dumps(all_attempts)[:3000],
    })

    # Status classification for metrics
    metrics["status"] = 429 if all_429 else 502
    metrics["error_type"] = error_subcategory
    metrics["key_cycle_attempts"] = key_cycle_attempts
    metrics["variant_fallback_attempts"] = variant_fallback_attempts
    metrics["key_cycle_attempt_types"] = [a.get("error_type", "429_rate_limit") for a in key_cycle_attempts]
    metrics["duration_ms"] = elapsed_ms
    metrics["timeout_exceeded_by_ms"] = elapsed_ms - UPSTREAM_TIMEOUT * 1000 if elapsed_ms > UPSTREAM_TIMEOUT * 1000 else 0

    # Return all-keys-exhausted result — handler formats per agent type
    result.success = False
    result.all_keys_exhausted = True
    result.all_429 = all_429
    result.all_non_quota_429 = all_non_quota_429 if all_429 else False
    result.has_500 = has_500
    result.has_502 = has_502
    result.has_timeout = has_timeout
    result.has_conn_err = has_conn_err
    result.error_subcategory = error_subcategory
    result.elapsed_ms = elapsed_ms
    result.key_cycle_attempts = key_cycle_attempts
    result.variant_idx = start_variant_idx
    result.litellm_model_base = litellm_model_base

    return result
