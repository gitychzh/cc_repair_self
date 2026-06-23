#!/usr/bin/env python3
"""HTTP handler: routes, request processing, agent-type dispatch.

R23 refactoring: handlers.py is now a slim dispatcher that delegates:
- Upstream v×k cycling to upstream.py (UpstreamResult)
- Format conversion to converters module (Anthropic path only)
- Streaming to stream module (Anthropic SSE path only)
- Error mapping to error_mapping module (both Anthropic and OpenAI formats)

R29: PROXY_ROLE determines which endpoints this proxy serves:
  - "cc"          → /v1/messages only (Anthropic format, CC)
  - "codex"       → /v1/responses only (Responses API, Codex)
  - "passthrough" → /v1/chat/completions only (OpenAI format, _ol/_oc/_hm_ms)

Three proxy containers each serve their own role:
  40001 (cc):          CC → Anthropic format → glm5.1 v×k cycling
  40002 (codex):       Codex → Responses API → glm5.1 v×k cycling
  40003 (passthrough): _ol/_oc/_hm_ms → OpenAI passthrough → glm5.1 v×k cycling
"""
import http.server
import json
import os
import time
import datetime
import uuid
import http.client
import urllib.parse
import socket

from .config import (
    LITELLM_KEY, PROXY_TIMEOUT, UPSTREAM_TIMEOUT, MODEL_MAP, DEFAULT_MODEL, DEFAULT_UPSTREAM_MODEL,
    MODEL_UPSTREAMS, MODEL_INPUT_TOKEN_SAFETY, DEFAULT_CONTEXT_FALLBACK,
    CHARS_PER_TOKEN_ESTIMATE, NUM_KEYS,
    AGENT_SUFFIXES, DEFAULT_AGENT_SUFFIX, detect_agent_type, format_model_id,
    PROXY_ROLE, ROLE_DEFAULT_UPSTREAM,
    _is_routing_name,
)
from .logger import _log, _log_metrics, _log_error_detail
from .converters import anth_to_openai, openai_to_anth, _estimate_text_chars
from .stream import stream_to_anth, collect_stream_to_anth
from .error_mapping import (
    convert_error, get_upstream_status_for_client,
    is_input_overflow, is_quota_exhaustion,
    format_openai_error_all_keys_exhausted,
    format_openai_error_upstream,
)
from .upstream import execute_request, UpstreamResult
from .codex import handle_codex_responses


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/"):
            gw_urls = {k: v["chat_url"] for k, v in MODEL_UPSTREAMS.items()}
            self._send_json(200, {
                "status": "ok",
                "proxy_role": PROXY_ROLE,
                "gateways": gw_urls,
                "port": int(os.environ.get("LISTEN_PORT", "40001")),
            })
        elif parsed.path == "/v1/models" or parsed.path == "/models":
            # Only serve models if this proxy role handles the relevant endpoint
            if PROXY_ROLE == "cc":
                # CC proxy: Anthropic-format models (for CC startup check)
                anth_version = self.headers.get("anthropic-version")
                if anth_version:
                    self._anthropic_models_list()
                else:
                    self._proxy_models()  # Fallback for non-Anthropic clients on CC port
            elif PROXY_ROLE == "passthrough":
                # Passthrough proxy: OpenAI-format models (for _ol/_oc/_hm_ms)
                self._proxy_models()
            elif PROXY_ROLE == "codex":
                # Codex proxy: OpenAI-format models (Codex checks /v1/models)
                self._proxy_models()
            else:
                self._proxy_models()
        elif parsed.path.startswith("/v1/models/") or parsed.path.startswith("/models/"):
            if PROXY_ROLE == "cc":
                model_id = parsed.path.split("/models/")[1].strip("/")
                self._anthropic_model_detail(model_id)
            else:
                self._send_json(404, {"error": "not found"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/", "/v1/models", "/models", "/v1/responses", "/responses") or parsed.path.startswith("/v1/models/") or parsed.path.startswith("/models/"):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        # Role-based endpoint filtering (R29)
        if PROXY_ROLE == "cc":
            # CC proxy: only /v1/messages (Anthropic format)
            if parsed.path == "/v1/messages":
                self._handle_messages()
            else:
                self._send_json(404, {"error": {"message": f"CC proxy only serves /v1/messages. Role={PROXY_ROLE}", "type": "invalid_request_error"}})
        elif PROXY_ROLE == "codex":
            # Codex proxy: only /v1/responses (Responses API format)
            if parsed.path in ("/v1/responses", "/responses"):
                self._handle_codex_responses()
            else:
                self._send_json(404, {"error": {"type": "invalid_request_error", "code": "404", "message": f"Codex proxy only serves /v1/responses. Role={PROXY_ROLE}"}})
        elif PROXY_ROLE == "passthrough":
            # Passthrough proxy: only /v1/chat/completions (OpenAI format)
            if parsed.path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_openai_with_cycling()
            else:
                self._send_json(404, {"error": {"message": f"Passthrough proxy only serves /v1/chat/completions. Role={PROXY_ROLE}", "type": "invalid_request_error", "code": "404"}})
        else:
            # Unknown role — serve all endpoints (legacy fallback)
            if parsed.path == "/v1/messages":
                self._handle_messages()
            elif parsed.path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_openai_with_cycling()
            elif parsed.path in ("/v1/responses", "/responses"):
                self._handle_codex_responses()
            else:
                self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ─── /v1/messages — Anthropic format request (CC / _cc) ───
    def _handle_messages(self):
        """Handle Anthropic-format requests from Claude Code (and other Anthropic-format agents).

        Flow:
          1. Parse Anthropic request body
          2. Detect agent type from model name (suffix or default _cc)
          3. Convert Anthropic → OpenAI format
          4. Apply force-stream-for-nonstream (ModelScope delta bug workaround)
          5. Call upstream.execute_request() with v×k cycling
          6. On success: convert OpenAI response → Anthropic format (stream or non-stream)
          7. On error: format Anthropic error response
        """
        t_start = time.time()
        request_id = str(uuid.uuid4())[:8]
        metrics = {
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "path": "/v1/messages",
            "proxy_role": PROXY_ROLE,
            "request_model": "?",
            "mapped_model": "?",
            "agent_type": "?",
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

        # Detect agent type from model name
        base_model, agent_suffix, response_format = detect_agent_type(request_model)
        metrics["agent_type"] = agent_suffix
        metrics["mapped_model"] = base_model
        _log("AGENT", f"model={request_model} → base={base_model} suffix={agent_suffix} format={response_format}")

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

        # Convert Anthropic → OpenAI
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

        # Input token estimation (metrics only, no proxy-level truncation)
        estimated_tokens = int(metrics["total_input_chars"] / CHARS_PER_TOKEN_ESTIMATE)
        estimated_tokens_json = int(metrics["total_input_chars_json"] / CHARS_PER_TOKEN_ESTIMATE)
        metrics["estimated_input_tokens"] = estimated_tokens
        metrics["estimated_input_tokens_json"] = estimated_tokens_json
        if estimated_tokens > 120000:
            _log("INPUT-WARN", f"estimated_tokens={estimated_tokens} (json_est={estimated_tokens_json}) — large context, CC auto-compact may trigger soon")

        # ─── ModelScope force-stream ───
        # Only force-stream for Anthropic path (CC) — OpenAI agents get proper non-stream responses
        force_stream_for_nonstream = (not is_stream)
        metrics["_original_stream"] = is_stream
        if force_stream_for_nonstream:
            oai_body["stream"] = True
            _log("FORCE-STREAM", f"non-stream → forcing stream=True (collect+synthesize)")

        _log("REQ", f"model={request_model}→{mapped_model} stream={is_stream} "
                    f"msgs={len(oai_body.get('messages',[]))} "
                    f"tools={len(oai_body.get('tools',[]))} "
                    f"agent={agent_suffix}")

        # ─── Execute upstream request with v×k cycling ───
        result = execute_request(self, oai_body, mapped_model, request_id, metrics, t_start)

        if not result.success:
            # ─── Error handling ───
            if result.all_keys_exhausted:
                # R35.6: Log metrics for ABORT path (previously Ghost-ABORT — metrics.jsonl
                # showed 100% status=200 while actual ABORTs happened, masking failures).
                metrics["status"] = 429 if result.all_429 else 502
                metrics["error_type"] = result.error_subcategory or "all_keys_exhausted"
                metrics["duration_ms"] = result.elapsed_ms
                metrics["key_cycle_attempts"] = len(result.key_cycle_attempts)
                metrics["key_cycle_details"] = result.key_cycle_attempts
                _log_metrics(metrics)

                if result.all_429 and not result.all_non_quota_429:
                    cycled_keys = ', '.join(['k' + str(a.get('key_idx', a.get('nv_key_idx', 0))+1) for a in result.key_cycle_attempts])
                    self._send_json(429, {
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": f"All {NUM_KEYS} ModelScope API keys have exhausted their token quota for model {mapped_model}. "
                                       f"Please wait for quota recovery (typically 15 minutes) before retrying. "
                                       f"Keys cycled: {cycled_keys}"
                        },
                        "model": request_model,
                    }, extra_headers={"retry-after": "180"})
                elif result.all_429 and result.all_non_quota_429:
                    cycled_keys = ', '.join(['k' + str(a.get('key_idx', a.get('nv_key_idx', 0))+1) for a in result.key_cycle_attempts])
                    self._send_json(429, {
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": f"All {NUM_KEYS} ModelScope API keys returned transient 429 errors for model {mapped_model}. "
                                       f"This is a temporary rate limit — not quota exhaustion. "
                                       f"Please retry in a few seconds. Keys cycled: {cycled_keys}"
                        },
                        "model": request_model,
                    }, extra_headers={"retry-after": "5"})
                else:
                    failure_types = [a.get("error_type", "429") for a in result.key_cycle_attempts]
                    timeout_keys = [f"k{a.get('key_idx', a.get('nv_key_idx', 0))+1}" for a in result.key_cycle_attempts if a.get("error_type") == "SocketTimeout"]
                    connerr_keys = [f"k{a.get('key_idx', a.get('nv_key_idx', 0))+1}" for a in result.key_cycle_attempts if a.get("error_type") in ("ConnectionRefusedError", "ConnectionError")]
                    self._send_json(502, {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"All {NUM_KEYS} key groups failed for model {mapped_model} after {result.elapsed_ms/1000:.1f}s. "
                                       f"Failure types: {failure_types}. "
                                       f"Timeout keys: {timeout_keys} (PROXY_TIMEOUT={PROXY_TIMEOUT}s). "
                                       f"Connection error keys: {connerr_keys}. "
                                       f"Please retry — upstream may recover.",
                        },
                        "model": request_model,
                    })
                return
            else:
                # Non-cycling upstream error (400, 401, etc)
                error_json = result.final_error_json
                resp_status = result.final_resp_status

                # Input overflow check
                if is_input_overflow(error_json, resp_status):
                    _log("INPUT-OVERFLOW", f"400 input overflow → invalid_request_error (CC stops, no compact)")
                    err_msg = json.dumps(error_json)[:500]
                    self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                         "message": f"Input tokens exceed ModelScope limit. Please start a new conversation. Detail: {err_msg}"},
                                         "model": request_model})
                    metrics["status"] = 400
                    metrics["error_type"] = "InputExceedsInvalidRequest"
                    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                    _log_metrics(metrics)
                    return

                client_status = get_upstream_status_for_client(resp_status)
                error_payload = convert_error(error_json, request_model)
                extra_hdrs = None
                if client_status == 429:
                    # R35.6: is_quota_exhaustion now always returns False (same as cc-proxy)
                    quota_exhaust = is_quota_exhaustion(error_json)
                    retry_seconds = 30 if quota_exhaust else 5
                    extra_hdrs = {"retry-after": str(retry_seconds)}
                    _log("RETRY-AFTER", f"429 rate_limit_error → retry-after={retry_seconds}s (quota={quota_exhaust})")
                elif client_status == 529:
                    extra_hdrs = {"retry-after": "5"}
                    _log("RETRY-AFTER", f"529 overloaded → retry-after=5s (api_error, CC retries then stops)")
                # R35.6: Log metrics for non-cycling error path
                metrics["status"] = client_status
                metrics["error_type"] = "upstream_error"
                metrics["error_message"] = str(error_json)[:200]
                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                _log_metrics(metrics)
                self._send_json(client_status, error_payload, extra_headers=extra_hdrs)
                return

        # ─── Success: process response ───
        resp = result.resp
        conn = result.conn
        metrics["key_idx"] = result.key_idx
        metrics["variant_idx"] = result.variant_idx
        metrics["litellm_model"] = result.litellm_model
        if result.key_cycle_attempts:
            metrics["key_cycle_429s_before_success"] = len(result.key_cycle_attempts)
            metrics["key_cycle_details"] = result.key_cycle_attempts

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

    # ─── /v1/chat/completions — OpenAI format request (_ol/_oc/_hm_ms) ───
    def _handle_openai_with_cycling(self):
        """Handle OpenAI-format requests from OpenClaw/OpenCode/Hermes.

        R29: These agents now route to glm5.1 backend via passthrough proxy (40003).
        The proxy does nearly-transparent passthrough with v×k cycling.

        Flow:
          1. Parse OpenAI request body
          2. Detect agent type from model name (suffix required for OpenAI agents)
          3. Map model name to backend (strip suffix → get base model)
          4. Call upstream.execute_request() with v×k cycling
          5. On success: pass through OpenAI response (no format conversion)
          6. On error: format OpenAI error response
        """
        t_start = time.time()
        request_id = str(uuid.uuid4())[:8]
        metrics = {
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "path": "/v1/chat/completions",
            "proxy_role": PROXY_ROLE,
            "request_model": "?",
            "mapped_model": "?",
            "agent_type": "?",
            "stream": False,
            "total_input_chars": 0,
            "ttfb_ms": None,
            "duration_ms": 0,
            "status": 0,
            "error_type": None,
            "error_message": None,
            "upstream": "?",
        }

        try:
            body_len = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(body_len) if body_len > 0 else b""
            body = json.loads(raw) if raw else {}
        except Exception as e:
            self._send_json(400, {"error": {"message": f"bad request: {e}", "type": "invalid_request_error", "code": "400"}})
            metrics["status"] = 400; metrics["error_type"] = "BadRequest"
            _log_metrics(metrics)
            return

        request_model = body.get("model", DEFAULT_MODEL)
        is_stream = body.get("stream", False)
        metrics["request_model"] = request_model
        metrics["stream"] = is_stream

        # Detect agent type from model name
        base_model, agent_suffix, response_format = detect_agent_type(request_model)
        metrics["agent_type"] = agent_suffix
        metrics["mapped_model"] = base_model

        # Map model name to backend
        mapped_model = MODEL_MAP.get(request_model, DEFAULT_MODEL)
        metrics["mapped_model"] = mapped_model

        # Input chars estimation for metrics
        json_chars = len(json.dumps(body))
        metrics["total_input_chars"] = json_chars

        _log("REQ", f"model={request_model}→{mapped_model} stream={is_stream} "
                    f"msgs={len(body.get('messages',[]))} "
                    f"agent={agent_suffix}")

        # ─── Execute upstream request with v×k cycling ───
        # For OpenAI agents: do NOT force-stream-for-nonstream
        # OpenAI agents expect proper non-stream responses
        metrics["_original_stream"] = is_stream

        # Add stream_options.include_usage for streaming (needed for metrics)
        if is_stream and "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}


        result = execute_request(self, body, mapped_model, request_id, metrics, t_start)

        if not result.success:
            # ─── Error handling for OpenAI format ───
            if result.all_keys_exhausted:
                # R35.6: Log metrics for ABORT path (Ghost-ABORT fix)
                metrics["status"] = 429 if result.all_429 else 502
                metrics["error_type"] = result.error_subcategory or "all_keys_exhausted"
                metrics["duration_ms"] = result.elapsed_ms
                metrics["key_cycle_attempts"] = len(result.key_cycle_attempts)
                metrics["key_cycle_details"] = result.key_cycle_attempts
                _log_metrics(metrics)

                error_payload, client_status = format_openai_error_all_keys_exhausted(result, mapped_model, request_model)
                extra_hdrs = None
                if client_status == 429:
                    retry_seconds = 5 if result.all_non_quota_429 else 180
                    extra_hdrs = {"retry-after": str(retry_seconds)}
                self._send_json(client_status, error_payload, extra_headers=extra_hdrs)
                return
            else:
                # Non-cycling upstream error — format OpenAI error
                error_json = result.final_error_json
                resp_status = result.final_resp_status
                error_payload, client_status = format_openai_error_upstream(error_json, request_model, resp_status)
                extra_hdrs = None
                if client_status == 429:
                    # R35.6: is_quota_exhaustion now always returns False (same as cc-proxy)
                    quota_exhaust = is_quota_exhaustion(error_json)
                    retry_seconds = 30 if quota_exhaust else 5
                    extra_hdrs = {"retry-after": str(retry_seconds)}
                # R35.6: Log metrics for non-cycling error path
                metrics["status"] = client_status
                metrics["error_type"] = "upstream_error"
                metrics["error_message"] = str(error_json)[:200]
                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                _log_metrics(metrics)
                self._send_json(client_status, error_payload, extra_headers=extra_hdrs)
                return

        # ─── Success: pass through OpenAI response ───
        resp = result.resp
        conn = result.conn
        metrics["key_idx"] = result.key_idx
        metrics["variant_idx"] = result.variant_idx
        metrics["litellm_model"] = result.litellm_model
        if result.key_cycle_attempts:
            metrics["key_cycle_429s_before_success"] = len(result.key_cycle_attempts)
            metrics["key_cycle_details"] = result.key_cycle_attempts

        if is_stream:
            # Streaming: pass SSE stream directly to client (no Anthropic conversion)
            self._stream_openai_passthrough(resp, conn, metrics, t_start, request_model)
        else:
            # Non-stream: read response body, pass through as-is
            ttfb_start = time.time()
            resp_body = resp.read()
            metrics["status"] = 200
            metrics["duration_ms"] = int((time.time() - t_start) * 1000)
            metrics["ttfb_ms"] = int((ttfb_start - t_start) * 1000)

            # Try to extract usage for metrics
            try:
                oai_response = json.loads(resp_body)
                usage = oai_response.get("usage", {})
                metrics["input_tokens"] = usage.get("prompt_tokens", 0)
                metrics["output_tokens"] = usage.get("completion_tokens", 0)
                choices = oai_response.get("choices", [])
                if choices:
                    metrics["finish_reason"] = choices[0].get("finish_reason")
            except Exception:
                pass

            _log_metrics(metrics)

            # Pass through the response body as-is
            self.send_response(resp.status)
            for h in ["Content-Type"]:
                v = resp.getheader(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()

    def _stream_openai_passthrough(self, resp, conn, metrics, t_start, request_model):
        """Pass through OpenAI streaming SSE response directly to client.

        Unlike CC's Anthropic SSE conversion (stream_to_anth), this is a simple
        byte-level passthrough — no format conversion needed for OpenAI agents.
        """
        ttfb_recorded = False
        streaming_input_tokens = 0
        streaming_output_tokens = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break

                if not ttfb_recorded:
                    metrics["ttfb_ms"] = int((time.time() - t_start) * 1000)
                    ttfb_recorded = True

                try:
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.split("\n"):
                        if line.startswith("data:") and line[5:].strip() != "[DONE]":
                            data_str = line[5:].strip()
                            if data_str:
                                data = json.loads(data_str)
                                chunk_usage = data.get("usage", {})
                                if chunk_usage:
                                    pt = chunk_usage.get("prompt_tokens", 0)
                                    ct = chunk_usage.get("completion_tokens", 0)
                                    if pt > 0:
                                        streaming_input_tokens = pt
                                    if ct > 0:
                                        streaming_output_tokens = ct
                except Exception:
                    pass  # Don't let metrics extraction break passthrough

                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except Exception:
                    break

        except (http.client.RemoteDisconnected, ConnectionResetError,
                OSError, http.client.IncompleteRead, socket.timeout) as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            error_class = type(e).__name__
            _log("ERR", f"OpenAI stream {error_class} after {elapsed_ms}ms: {e}")
            _log_error_detail({
                "request_id": metrics.get("request_id", "?"),
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": f"openai_stream_{error_class}",
                "elapsed_since_request_start_ms": elapsed_ms,
                "error_message": str(e)[:300],
            })
            # R35.6: Set error_type so metrics status reflects actual outcome (not always 200)
            metrics["error_type"] = f"OpenAIStream_{error_class}"
        except Exception as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            error_class = type(e).__name__
            _log("ERR", f"OpenAI stream unexpected {error_class} after {elapsed_ms}ms: {e}")
            # R35.6: Set error_type for unexpected stream errors
            metrics["error_type"] = f"OpenAIStream_{error_class}"

        # R35.6: metrics status now reflects actual outcome, not always 200.
        # Ghost-Stream bug: stream error → status=200 masked failures in metrics.
        if metrics.get("error_type"):
            metrics["status"] = 502  # Stream error — client received incomplete response
        else:
            metrics["status"] = 200  # Clean completion
        metrics["duration_ms"] = int((time.time() - t_start) * 1000)
        if streaming_input_tokens > 0:
            metrics["input_tokens"] = streaming_input_tokens
        if streaming_output_tokens > 0:
            metrics["output_tokens"] = streaming_output_tokens
        _log_metrics(metrics)

        try:
            conn.close()
        except Exception:
            pass

    # ─── /v1/responses — Responses API format request (Codex / _cx) ───
    def _handle_codex_responses(self):
        """Handle Responses API requests from Codex CLI.

        Flow:
          1. Parse Responses API request body
          2. Detect agent type from model name
          3. Map model name to backend
          4. Delegate to codex.handle_codex_responses()
        """
        t_start = time.time()
        request_id = str(uuid.uuid4())[:8]
        metrics = {
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "path": "/v1/responses",
            "proxy_role": PROXY_ROLE,
            "request_model": "?",
            "mapped_model": "?",
            "agent_type": "_cx",
            "stream": False,
            "total_input_chars": 0,
            "ttfb_ms": None,
            "duration_ms": 0,
            "status": 0,
            "error_type": None,
            "error_message": None,
            "upstream": "?",
        }

        try:
            body_len = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(body_len) if body_len > 0 else b""
            cx_body = json.loads(raw) if raw else {}
        except Exception as e:
            self._send_json(400, {"error": {"type": "invalid_request_error", "code": "400",
                                           "message": f"bad request: {e}"}})
            metrics["status"] = 400; metrics["error_type"] = "BadRequest"
            _log_metrics(metrics)
            return

        request_model = cx_body.get("model", DEFAULT_MODEL)
        metrics["request_model"] = request_model

        # Detect agent type and map model
        base_model, agent_suffix, response_format = detect_agent_type(request_model)
        mapped_model = MODEL_MAP.get(request_model, DEFAULT_MODEL)
        metrics["mapped_model"] = mapped_model

        _log("AGENT", f"model={request_model} → base={base_model} suffix={agent_suffix} format={response_format}")

        # Delegate to codex module for full handling
        handle_codex_responses(self, cx_body, mapped_model, request_model, request_id, metrics, t_start)

    # ─── /v1/models proxy ───
    def _proxy_models(self):
        """Return OpenAI-format model list.

        R29: Shows models appropriate for this proxy's role:
          cc: glm5.1_cc + backward compat aliases
          codex: glm5.1_cx + backward compat aliases
          passthrough: glm5.1_ol/glm5.1_oc/glm5.1_hm_ms
        """
        all_models = []
        seen_ids = set()

        # Include suffix-based model IDs for each base model × agent type
        for base_model in MODEL_UPSTREAMS:
            for suffix, info in AGENT_SUFFIXES.items():
                model_id = format_model_id(base_model, suffix)
                if model_id not in seen_ids:
                    seen_ids.add(model_id)
                    # R31.4: advertise SAFE capacity (not the hard ceiling) so clients
                    # don't fill up to the ModelScope limit and hit 400. Matches the
                    # Anthropic /v1/models context_window value for the same model.
                    context_len = MODEL_INPUT_TOKEN_SAFETY.get(base_model, DEFAULT_CONTEXT_FALLBACK)
                    all_models.append({
                        "id": model_id,
                        "object": "model",
                        "created": 0,
                        "owned_by": info["name"],
                        "context_length": context_len,
                    })

        # Also include upstream LiteLLM models (filtering routing names)
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
                    if _is_routing_name(model_id):
                        continue
                    if model_id in seen_ids or model_id in MODEL_UPSTREAMS:
                        continue
                    if model_id not in seen_ids:
                        seen_ids.add(model_id)
                        upstream_key = MODEL_MAP.get(model_id, model_id)
                        context_len = MODEL_INPUT_TOKEN_SAFETY.get(upstream_key, DEFAULT_CONTEXT_FALLBACK)
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

        # Include base model names if not covered
        for model_key in MODEL_UPSTREAMS:
            if model_key not in seen_ids:
                seen_ids.add(model_key)
                context_len = MODEL_INPUT_TOKEN_SAFETY.get(model_key, DEFAULT_CONTEXT_FALLBACK)
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
        """Return Anthropic-format model list with context_window."""
        all_models = []
        seen_ids = set()

        # Include _cc suffix model IDs for each base model
        for base_model in MODEL_UPSTREAMS:
            model_id = format_model_id(base_model, "_cc")
            if model_id not in seen_ids:
                seen_ids.add(model_id)
                safety = MODEL_INPUT_TOKEN_SAFETY.get(base_model, DEFAULT_CONTEXT_FALLBACK)
                all_models.append({
                    "id": model_id,
                    "type": "model",
                    "display_name": base_model,
                    "created_at": "2024-01-01T00:00:00Z",
                    "context_window": safety,
                })

        # Include ALL model IDs from MODEL_MAP (backward compat)
        for model_id, mapped in MODEL_MAP.items():
            if model_id not in seen_ids:
                seen_ids.add(model_id)
                safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, DEFAULT_CONTEXT_FALLBACK)
                display = format_model_id(mapped, "_cc") if not model_id.endswith("_cc") else model_id
                all_models.append({
                    "id": model_id,
                    "type": "model",
                    "display_name": display,
                    "created_at": "2024-01-01T00:00:00Z",
                    "context_window": safety,
                })

        # Include base model names if not covered
        for model_key in MODEL_UPSTREAMS:
            if model_key not in seen_ids:
                seen_ids.add(model_key)
                safety = MODEL_INPUT_TOKEN_SAFETY.get(model_key, DEFAULT_CONTEXT_FALLBACK)
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
        safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, DEFAULT_CONTEXT_FALLBACK)
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
        return http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT)

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
