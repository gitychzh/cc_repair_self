#!/usr/bin/env python3
"""HTTP handler: routes, request processing, resilience retry, thinking_budget fix.

ProxyHandler extends http.server.BaseHTTPRequestHandler and delegates:
- Format conversion to converters module
- Streaming to stream module
- Error mapping to error_mapping module
- Logging to logger module
"""
import http.server
import json
import os
import re
import time
import datetime
import uuid
import http.client
import urllib.parse

from .config import (
    LITELLM_KEY, PROXY_TIMEOUT, MODEL_MAP, DEFAULT_MODEL, DEFAULT_UPSTREAM_MODEL,
    MODEL_UPSTREAMS, MODEL_MAX_INPUT_TOKENS, MODEL_INPUT_TOKEN_SAFETY,
    CHARS_PER_TOKEN_ESTIMATE, OUTPUT_TOKEN_MARGIN,
    NUM_KEYS, _next_key_idx, _is_key_group_name,
)
from .logger import _log, _log_metrics, _log_error_detail
from .converters import anth_to_openai, openai_to_anth, _estimate_text_chars
from .stream import stream_to_anth, collect_stream_to_anth
from .error_mapping import convert_error, get_upstream_status_for_client, is_input_overflow, is_quota_exhaustion


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/"):
            gw_urls = {k: v["chat_url"] for k, v in MODEL_UPSTREAMS.items()}
            self._send_json(200, {
                "status": "ok",
                "proxy": "anthropic-to-openai",
                "gateways": gw_urls,
                "port": int(os.environ.get("LISTEN_PORT", "40001")),
            })
        elif parsed.path == "/v1/models" or parsed.path == "/models":
            # Check if this is a Anthropic-format request (has anthropic-version header)
            anth_version = self.headers.get("anthropic-version")
            if anth_version:
                self._anthropic_models_list()
            else:
                self._proxy_models()
        elif parsed.path.startswith("/v1/models/") or parsed.path.startswith("/models/"):
            # Anthropic model detail endpoint: /v1/models/{model_id}
            model_id = parsed.path.split("/models/")[1].strip("/")
            self._anthropic_model_detail(model_id)
        else:
            self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/", "/v1/models", "/models") or parsed.path.startswith("/v1/models/") or parsed.path.startswith("/models/"):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/messages":
            self._handle_messages()
        elif parsed.path in ("/v1/chat/completions", "/chat/completions"):
            self._passthrough_openai()
        else:
            self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ─── /v1/messages — Anthropic format request ───
    def _handle_messages(self):
        t_start = time.time()
        request_id = str(uuid.uuid4())[:8]
        metrics = {
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "path": "/v1/messages",
            "request_model": "?",
            "mapped_model": "?",
            "stream": False,
            "num_messages": 0,
            "num_tools": 0,
            "system_prompt_chars": 0,
            "total_input_chars": 0,
            "ttfb_ms": None,
            "duration_ms": 0,
            "status": 0,
            "finish_reason": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "tool_truncation": None,
            "error_type": None,
            "error_message": None,
            "upstream": "?",
        }

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            anth_body = json.loads(raw_body)
        except Exception as e:
            self._send_json(400, {"error": {"message": f"bad request: {e}"}})
            metrics["status"] = 400; metrics["error_type"] = "BadRequest"; metrics["error_message"] = str(e)
            _log("ERROR", f"bad request: {e}")
            _log_metrics(metrics)
            return

        request_model = anth_body.get("model", DEFAULT_MODEL)
        is_stream = anth_body.get("stream", False)
        metrics["request_model"] = request_model
        metrics["stream"] = is_stream

        # Track system prompt size
        system_blocks = anth_body.get("system")
        if system_blocks:
            if isinstance(system_blocks, str):
                metrics["system_prompt_chars"] = len(system_blocks)
            elif isinstance(system_blocks, list):
                metrics["system_prompt_chars"] = sum(
                    len(b.get("text", "")) if isinstance(b, dict) else len(b)
                    for b in system_blocks
                )

        # Convert Anthropic → OpenAI (map Claude model names to LiteLLM model_name)
        mapped_model = MODEL_MAP.get(request_model, DEFAULT_MODEL)
        oai_body = anth_to_openai(anth_body, target_model=mapped_model)
        metrics["mapped_model"] = mapped_model
        metrics["num_messages"] = len(oai_body.get("messages", []))
        metrics["num_tools"] = len(oai_body.get("tools", []))
        text_chars = _estimate_text_chars(oai_body)
        json_chars = len(json.dumps(oai_body))
        metrics["total_input_chars"] = text_chars
        metrics["total_input_chars_json"] = json_chars
        metrics["text_vs_json_ratio"] = round(text_chars / json_chars, 2) if json_chars > 0 else 0

        # Track tool truncation
        if metrics["num_tools"] > 0:
            total_orig = sum(len(t.get("description", "")) for t in anth_body.get("tools", [])
                            if t.get("type", "tool_use") == "tool_use")
            total_trunc = sum(len(t.get("function", {}).get("description", ""))
                            for t in oai_body.get("tools", [])
                            if t.get("type") == "function")
            metrics["tool_truncation"] = {
                "original_total_chars": total_orig,
                "truncated_total_chars": total_trunc,
                "reduction_pct": round((1 - total_trunc / total_orig) * 100, 1) if total_orig > 0 else 0,
                "num_tools": metrics["num_tools"],
            }

        # Select upstream
        upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
        upstream = MODEL_UPSTREAMS[upstream_key]
        litellm_url = upstream["chat_url"]
        metrics["upstream"] = upstream_key

        # ─── Input token estimation (metrics only, no proxy-level truncation) ───
        estimated_tokens = int(metrics["total_input_chars"] / CHARS_PER_TOKEN_ESTIMATE)
        estimated_tokens_json = int(metrics["total_input_chars_json"] / CHARS_PER_TOKEN_ESTIMATE)
        metrics["estimated_input_tokens"] = estimated_tokens
        metrics["estimated_input_tokens_json"] = estimated_tokens_json
        if estimated_tokens > 120000:
            _log("INPUT-WARN", f"estimated_tokens={estimated_tokens} (json_est={estimated_tokens_json}) — large context, CC auto-compact may trigger soon")

        # ─── ModelScope force-stream ───
        force_stream_for_nonstream = (not is_stream)
        if force_stream_for_nonstream:
            oai_body["stream"] = True
            _log("FORCE-STREAM", f"non-stream → forcing stream=True (collect+synthesize)")

        _log("REQ", f"model={request_model}→{mapped_model} stream={is_stream} "
                    f"msgs={len(oai_body.get('messages',[]))} "
                    f"tools={len(oai_body.get('tools',[]))}")

        # ─── Key round-robin + 429 cycling (R19) ───
        # LiteLLM config has 7 key groups per model (glm5.1k1~k7, dsv4pk1~k7).
        # Proxy round-robins: request N → key_idx = counter % NUM_KEYS → model "glm5.1k{idx+1}"
        # On 429 from a key group, cycle to next key group.
        # After all key groups return 429 → return 429 to agent (all keys exhausted).
        start_key_idx = _next_key_idx(mapped_model)
        litellm_model_base = mapped_model
        key_cycle_attempts = []

        for attempt_idx in range(NUM_KEYS):
            current_key_idx = (start_key_idx + attempt_idx) % NUM_KEYS
            litellm_model = f"{litellm_model_base}k{current_key_idx + 1}"
            oai_body["model"] = litellm_model
            _log("KEY-RR", f"attempt {attempt_idx+1}/{NUM_KEYS}: key_idx={current_key_idx} → model={litellm_model}")

            auth_key = self.headers.get("x-api-key") or self.headers.get("X-Api-Key") or LITELLM_KEY
            headers_out = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth_key}",
                "Content-Length": str(len(json.dumps(oai_body).encode("utf-8"))),
            }
            oai_data = json.dumps(oai_body).encode("utf-8")
            parsed_upstream = urllib.parse.urlparse(litellm_url)

            try:
                conn = self._make_upstream_conn(parsed_upstream)
                conn.request("POST", parsed_upstream.path, body=oai_data, headers=headers_out)
                resp = conn.getresponse()

                # Extract LiteLLM routing/quota headers for optimization analytics
                for hdr_key, metrics_key in [
                    ("x-litellm-model-id", "litellm_model_id"),
                    ("x-litellm-response-duration-ms", "litellm_response_duration_ms"),
                ]:
                    val = resp.getheader(hdr_key)
                    if val:
                        metrics[metrics_key] = val

                # Extract ModelScope quota headers from LiteLLM-passed llm_provider-* headers
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

                    # ─── 429 → cycle to next key (R19 key round-robin) ───
                    if resp.status == 429:
                        key_cycle_attempts.append({
                            "key_idx": current_key_idx,
                            "litellm_model": litellm_model,
                            "error_body": err_str[:500],
                        })
                        _log_error_detail({
                            "request_id": request_id,
                            "timestamp": datetime.datetime.now().isoformat(),
                            "error_subcategory": "429_key_cycle_attempt",
                            "upstream_status": 429,
                            "key_idx": current_key_idx,
                            "litellm_model": litellm_model,
                            "attempt_number": attempt_idx + 1,
                            "total_keys": NUM_KEYS,
                            "upstream_error_body_full": err_str[:3000],
                        })
                        _log("KEY-429", f"key {current_key_idx+1}/{NUM_KEYS} ({litellm_model}) → 429, cycling to next key")
                        continue

                    # ─── Non-429 errors: resilience retry ───
                    should_resilience_retry = (
                        resp.status in (401, 403)
                        and "AuthenticationError" in err_str
                        and metrics.get("_resilience_retry_count", 0) < 1
                    )
                    should_fix_thinking_budget = (
                        resp.status == 400
                        and "InvalidParameter" in err_str
                        and "thinking_budget" in err_str
                        and "max_completion_tokens" in err_str
                        and metrics.get("_thinking_budget_retry_count", 0) < 1
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
                                conn_fix = self._make_upstream_conn(parsed_upstream)
                                conn_fix.request("POST", parsed_upstream.path, body=fixed_data, headers=fixed_headers)
                                resp_fix = conn_fix.getresponse()
                                if resp_fix.status < 400:
                                    if is_stream:
                                        stream_to_anth(self, resp_fix, request_model, mapped_model, conn_fix, metrics, t_start)
                                        metrics["status"] = 200
                                        metrics["thinking_budget_fix_success"] = True
                                        metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                        _log_metrics(metrics)
                                        return
                                    elif force_stream_for_nonstream:
                                        collect_stream_to_anth(self, resp_fix, request_model, mapped_model, conn_fix, metrics, t_start)
                                        metrics["thinking_budget_fix_success"] = True
                                        return
                                    else:
                                        ttfb_fix = time.time()
                                        resp_body_fix = resp_fix.read()
                                        oai_resp_fix = json.loads(resp_body_fix)
                                        anth_resp_fix = openai_to_anth(oai_resp_fix, request_model)
                                        metrics["status"] = 200
                                        metrics["thinking_budget_fix_success"] = True
                                        metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                        metrics["ttfb_ms"] = int((ttfb_fix - t_start) * 1000)
                                        _log_metrics(metrics)
                                        self._send_json(200, anth_resp_fix)
                                        conn_fix.close()
                                        return
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
                            conn2 = self._make_upstream_conn(parsed_upstream)
                            conn2.request("POST", parsed_upstream.path, body=oai_data, headers=headers_out)
                            resp2 = conn2.getresponse()
                            if resp2.status < 400:
                                if is_stream:
                                    stream_to_anth(self, resp2, request_model, mapped_model, conn2, metrics, t_start)
                                    metrics["status"] = 200
                                    metrics["resilience_retry_success"] = True
                                    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                    _log_metrics(metrics)
                                    return
                                elif force_stream_for_nonstream:
                                    collect_stream_to_anth(self, resp2, request_model, mapped_model, conn2, metrics, t_start)
                                    metrics["resilience_retry_success"] = True
                                    return
                                else:
                                    ttfb_start2 = time.time()
                                    resp_body2 = resp2.read()
                                    oai_response2 = json.loads(resp_body2)
                                    anth_response2 = openai_to_anth(oai_response2, request_model)
                                    metrics["status"] = 200
                                    metrics["resilience_retry_success"] = True
                                    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                    metrics["ttfb_ms"] = int((ttfb_start2 - t_start) * 1000)
                                    usage2 = oai_response2.get("usage", {})
                                    metrics["input_tokens"] = usage2.get("prompt_tokens", 0)
                                    metrics["output_tokens"] = usage2.get("completion_tokens", 0)
                                    choices2 = oai_response2.get("choices", [])
                                    if choices2:
                                        metrics["finish_reason"] = choices2[0].get("finish_reason")
                                    _log_metrics(metrics)
                                    self._send_json(200, anth_response2)
                                    conn2.close()
                                    return
                            error_body2 = resp2.read()
                            try:
                                error_json2 = json.loads(error_body2)
                            except Exception:
                                error_json2 = {"error": error_body2.decode("utf-8", errors="replace")}
                            conn2.close()
                            error_json = error_json2
                            resp_status_final = resp2.status
                            _log("ERR", f"resilience retry also failed: {resp2.status} {json.dumps(error_json2)[:200]}")
                        except Exception as e2:
                            _log("ERR", f"resilience retry connection error: {e2}")
                            resp_status_final = resp.status
                    else:
                        resp_status_final = resp.status

                    # ─── Non-429, non-retryable (or retry failed) → report error ───
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

                    if is_input_overflow(error_json, resp_status_final):
                        _log("INPUT-OVERFLOW", f"400 input overflow → invalid_request_error (CC stops, no compact)")
                        err_msg = json.dumps(error_json)[:500]
                        self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                         "message": f"Input tokens exceed ModelScope limit. Please start a new conversation. Detail: {err_msg}"},
                                         "model": request_model})
                        metrics["status"] = 400
                        metrics["error_type"] = "InputExceedsInvalidRequest"
                        return

                    client_status = get_upstream_status_for_client(resp_status_final)
                    error_payload = convert_error(error_json, request_model)
                    extra_hdrs = None
                    if client_status == 429:
                        quota_exhaust = is_quota_exhaustion(error_json)
                        retry_seconds = 30 if quota_exhaust else 5
                        extra_hdrs = {"retry-after": str(retry_seconds)}
                        _log("RETRY-AFTER", f"429 rate_limit_error → retry-after={retry_seconds}s (quota={quota_exhaust})")
                    elif client_status == 529:
                        extra_hdrs = {"retry-after": "5"}
                        _log("RETRY-AFTER", f"529 overloaded → retry-after=5s (api_error, CC retries then stops)")
                    self._send_json(client_status, error_payload, extra_headers=extra_hdrs)
                    return

                # ─── Success: resp.status < 400 ───
                metrics["key_idx"] = current_key_idx
                metrics["litellm_model"] = litellm_model
                if key_cycle_attempts:
                    metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                    metrics["key_cycle_details"] = key_cycle_attempts
                    _log("KEY-CYCLE-SUCCESS", f"429 on {len(key_cycle_attempts)} key(s) before success on key {current_key_idx+1} ({litellm_model})")

                if is_stream:
                    stream_to_anth(self, resp, request_model, mapped_model, conn, metrics, t_start)
                elif force_stream_for_nonstream:
                    collect_stream_to_anth(self, resp, request_model, mapped_model, conn, metrics, t_start)
                else:
                    ttfb_start = time.time()
                    resp_body = resp.read()
                    oai_response = json.loads(resp_body)
                    anth_response = openai_to_anth(oai_response, request_model)
                    metrics["status"] = 200
                    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                    metrics["ttfb_ms"] = int((ttfb_start - t_start) * 1000)
                    usage = oai_response.get("usage", {})
                    metrics["input_tokens"] = usage.get("prompt_tokens", 0)
                    metrics["output_tokens"] = usage.get("completion_tokens", 0)
                    choices = oai_response.get("choices", [])
                    if choices:
                        metrics["finish_reason"] = choices[0].get("finish_reason")
                    _log_metrics(metrics)
                    self._send_json(200, anth_response)
                    conn.close()
                return  # Success — done with this request

            except Exception as e:
                _log("ERR", f"key {current_key_idx+1} ({litellm_model}) connection error: {e}")
                key_cycle_attempts.append({
                    "key_idx": current_key_idx,
                    "litellm_model": litellm_model,
                    "error": str(e)[:200],
                    "error_type": "ConnectionError",
                })
                continue  # Try next key

        # ─── All keys exhausted (429 on all NUM_KEYS) or all had connection errors ───
        _log("ALL-KEYS-429", f"All {NUM_KEYS} key groups exhausted for {mapped_model}. "
                             f"Cycled: {[a.get('litellm_model') for a in key_cycle_attempts]}")
        _log_error_detail({
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "error_subcategory": "429_all_keys_exhausted",
            "upstream_status": 429,
            "model": mapped_model,
            "total_keys": NUM_KEYS,
            "key_cycle_attempts": key_cycle_attempts,
            "upstream_error_body_full": json.dumps(key_cycle_attempts)[:3000],
        })
        metrics["status"] = 429
        metrics["error_type"] = "AllKeysExhausted"
        metrics["error_message"] = f"All {NUM_KEYS} ModelScope keys exhausted for model {mapped_model}."
        metrics["key_cycle_attempts"] = key_cycle_attempts
        metrics["duration_ms"] = int((time.time() - t_start) * 1000)
        _log_metrics(metrics)

        self._send_json(429, {
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": f"All {NUM_KEYS} ModelScope API keys have exhausted their token quota for model {mapped_model}. "
                           f"Please wait for quota recovery (typically 15 minutes) before retrying. "
                           f"Keys cycled: {', '.join(['k' + str(a['key_idx']+1) for a in key_cycle_attempts])}"
            },
            "model": request_model,
        }, extra_headers={"retry-after": "30"})

    # ─── Passthrough for OpenAI format requests (with key round-robin) ───
    def _passthrough_openai(self):
        body_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(body_len) if body_len > 0 else b""
        body = json.loads(raw) if raw else {}
        mapped_model = MODEL_MAP.get(body.get("model", DEFAULT_MODEL), DEFAULT_MODEL)
        upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
        upstream = MODEL_UPSTREAMS[upstream_key]
        litellm_url = upstream["chat_url"]

        start_key_idx = _next_key_idx(mapped_model)
        key_cycle_attempts = []

        for attempt_idx in range(NUM_KEYS):
            current_key_idx = (start_key_idx + attempt_idx) % NUM_KEYS
            litellm_model = f"{mapped_model}k{current_key_idx + 1}"
            body["model"] = litellm_model
            _log("KEY-RR-PASSTHRU", f"attempt {attempt_idx+1}/{NUM_KEYS}: key_idx={current_key_idx} → model={litellm_model}")

            forwarded_body = json.dumps(body).encode("utf-8")
            headers_out = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LITELLM_KEY}",
                "Content-Length": str(len(forwarded_body)),
            }
            parsed = urllib.parse.urlparse(litellm_url)
            try:
                conn = self._make_upstream_conn(parsed)
                conn.request("POST", parsed.path, body=forwarded_body, headers=headers_out)
                resp = conn.getresponse()
                resp_body = resp.read()

                if resp.status == 429:
                    key_cycle_attempts.append({
                        "key_idx": current_key_idx,
                        "litellm_model": litellm_model,
                        "error_body": resp_body.decode("utf-8", errors="replace")[:500],
                    })
                    conn.close()
                    _log("KEY-429-PASSTHRU", f"key {current_key_idx+1}/{NUM_KEYS} ({litellm_model}) → 429, cycling")
                    continue

                self.send_response(resp.status)
                for h in ["Content-Type"]:
                    v = resp.getheader(h)
                    if v:
                        self.send_header(h, v)
                self.end_headers()
                self.wfile.write(resp_body)
                conn.close()
                return

            except Exception as e:
                _log("ERR", f"passthru key {current_key_idx+1} ({litellm_model}) connection error: {e}")
                key_cycle_attempts.append({
                    "key_idx": current_key_idx,
                    "litellm_model": litellm_model,
                    "error": str(e)[:200],
                })
                continue

        _log("ALL-KEYS-429-PASSTHRU", f"All {NUM_KEYS} key groups exhausted in passthru for {mapped_model}")
        self._send_json(429, {
            "error": {
                "type": "rate_limit_error",
                "message": f"All {NUM_KEYS} ModelScope API keys exhausted for model {mapped_model}. Please wait for quota recovery."
            }
        }, extra_headers={"retry-after": "30"})

    # ─── /v1/models proxy ───
    def _proxy_models(self):
        all_models = []
        seen_ids = set()
        for model_key, upstream in MODEL_UPSTREAMS.items():
            models_url = upstream["models_url"]
            parsed = urllib.parse.urlparse(models_url)
            try:
                conn = self._make_upstream_conn(parsed)
                conn.request("GET", parsed.path or "/v1/models", headers={"Authorization": f"Bearer {LITELLM_KEY}"})
                resp = conn.getresponse()
                if resp.status != 200:
                    conn.close()
                    continue
                data = json.loads(resp.read())
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    # R19: Filter out key group internal names (e.g. "glm5.1k1", "dsv4pk3")
                    # These are proxy→LiteLLM routing names, not meant for CC/agents
                    if _is_key_group_name(model_id):
                        continue
                    if model_id not in seen_ids:
                        seen_ids.add(model_id)
                        upstream_key = MODEL_MAP.get(model_id, model_id)
                        context_len = MODEL_MAX_INPUT_TOKENS.get(upstream_key, 131072)
                        all_models.append({
                            "id": model_id,
                            "object": "model",
                            "created": m.get("created", 0),
                            "owned_by": m.get("owned_by", ""),
                            "context_length": context_len,
                        })
                conn.close()
            except Exception as e:
                _log("ERROR", f"models proxy error for {model_key}: {e}")
        # R19: Always include canonical names (glm5.1, dsv4p) even if LiteLLM only lists key groups
        for model_key in MODEL_UPSTREAMS:
            if model_key not in seen_ids:
                seen_ids.add(model_key)
                context_len = MODEL_MAX_INPUT_TOKENS.get(model_key, 131072)
                all_models.append({
                    "id": model_key,
                    "object": "model",
                    "created": 0,
                    "owned_by": "proxy",
                    "context_length": context_len,
                })
        self._send_json(200, {"object": "list", "data": all_models})

    # ─── Anthropic-format /v1/models endpoints ───
    def _anthropic_models_list(self):
        """Return Anthropic-format model list with context_window.
        CC uses this context_window to decide when to trigger built-in auto-compact.
        Multi-agent compatibility: returns ALL model aliases from MODEL_MAP,
        so other agents (OpenCode, Codex) can discover requestable model IDs.
        """
        all_models = []
        seen_ids = set()
        for model_id, mapped in MODEL_MAP.items():
            if model_id not in seen_ids:
                seen_ids.add(model_id)
                safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, 128000)
                all_models.append({
                    "id": model_id,
                    "type": "model",
                    "display_name": mapped,
                    "created_at": "2024-01-01T00:00:00Z",
                    "context_window": safety,
                })
        for model_key in MODEL_UPSTREAMS:
            if model_key not in seen_ids:
                seen_ids.add(model_key)
                safety = MODEL_INPUT_TOKEN_SAFETY.get(model_key, 128000)
                all_models.append({
                    "id": model_key,
                    "type": "model",
                    "display_name": model_key,
                    "created_at": "2024-01-01T00:00:00Z",
                    "context_window": safety,
                })
        self._send_json(200, {"data": all_models, "has_more": False})

    def _anthropic_model_detail(self, model_id):
        """Return Anthropic-format model detail for a specific model ID."""
        mapped = MODEL_MAP.get(model_id, DEFAULT_MODEL)
        safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, 128000)
        self._send_json(200, {
            "id": model_id,
            "type": "model",
            "display_name": mapped,
            "created_at": "2024-01-01T00:00:00Z",
            "context_window": safety,
        })

    # ─── Helpers ───
    def _make_upstream_conn(self, parsed_url):
        host = parsed_url.hostname
        port = parsed_url.port or 80
        return http.client.HTTPConnection(host, port, timeout=PROXY_TIMEOUT)

    def _send_json(self, code, data, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_raw(code, body, "application/json", extra_headers)

    def _send_raw(self, code, body_bytes, content_type="application/json", extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_sse(self, event_type, data_dict):
        data_str = json.dumps(data_dict, ensure_ascii=False)
        msg = f"event: {event_type}\ndata: {data_str}\n\n"
        try:
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, fmt, *args):
        pass  # Suppress default logging