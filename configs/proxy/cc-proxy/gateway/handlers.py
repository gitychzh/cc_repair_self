#!/usr/bin/env python3
"""CC-proxy HTTP handler — Anthropic format only (/v1/messages).

R31.5: This is the cc-role gateway, physically isolated from the codex/
passthrough gateways. The old "one shared image + PROXY_ROLE switch" design
caused cross-container breakage (a change for one role affected all three).
This file serves ONLY Claude Code (Anthropic /v1/messages → glm5.1 v×k).

Delegation:
- Upstream v×k cycling     → upstream.py
- Format conversion        → converters.py (Anthropic↔OpenAI)
- Streaming                → stream.py (Anthropic SSE)
- Error mapping            → error_mapping.py (Anthropic format only here)
"""
import http.server
import json
import os
import time
import datetime
import uuid
import http.client
import urllib.parse

from .config import (
    PROXY_TIMEOUT, UPSTREAM_TIMEOUT, MODEL_MAP, DEFAULT_MODEL,
    MODEL_UPSTREAMS, MODEL_INPUT_TOKEN_SAFETY, DEFAULT_CONTEXT_FALLBACK,
    CHARS_PER_TOKEN_ESTIMATE, NUM_KEYS, NV_ENABLED,
    detect_agent_type,
    PROXY_ROLE,
)
from .logger import _log, _log_metrics, _log_error_detail
from .converters import anth_to_openai, openai_to_anth, _estimate_text_chars
from .stream import stream_to_anth, collect_stream_to_anth
from .error_mapping import (
    convert_error, get_upstream_status_for_client,
    is_input_overflow, is_quota_exhaustion,
)
from .upstream import execute_request, UpstreamResult


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
        elif parsed.path in ("/v1/models", "/models"):
            # CC startup check sends anthropic-version header → Anthropic format.
            # Non-Anthropic clients on this port get a plain Anthropic list too
            # (this is a CC-only proxy; no OpenAI-format model listing here).
            self._anthropic_models_list()
        elif parsed.path.startswith("/v1/models/") or parsed.path.startswith("/models/"):
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
        # CC proxy serves only /v1/messages (Anthropic format).
        if parsed.path == "/v1/messages":
            self._handle_messages()
        else:
            self._send_json(404, {"error": {"message": f"CC proxy only serves /v1/messages. Role={PROXY_ROLE}", "type": "invalid_request_error"}})

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
                if result.all_429 and not result.all_non_quota_429:
                    cycled_keys = ', '.join([f"k{a.get('key_idx',a.get('nv_key_idx',0))+1}" for a in result.key_cycle_attempts])
                    self._send_json(429, {
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": f"All ModelScope/NVIDIA API keys have exhausted their quota for model {mapped_model}. "
                                       f"Please wait for quota recovery (typically 15 minutes) before retrying. "
                                       f"Keys cycled: {cycled_keys}"
                        },
                        "model": request_model,
                    }, extra_headers={"retry-after": "180"})
                elif result.all_429 and result.all_non_quota_429:
                    cycled_keys = ', '.join([f"k{a.get('key_idx',a.get('nv_key_idx',0))+1}" for a in result.key_cycle_attempts])
                    self._send_json(429, {
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": f"All API keys returned transient 429 errors for model {mapped_model}. "
                                       f"This is a temporary rate limit — not quota exhaustion. "
                                       f"Please retry in a few seconds. Keys cycled: {cycled_keys}"
                        },
                        "model": request_model,
                    }, extra_headers={"retry-after": "10"})
                else:
                    failure_types = [a.get("error_type", "429") for a in result.key_cycle_attempts]
                    timeout_keys = [f"k{a.get('key_idx',a.get('nv_key_idx',0))+1}" for a in result.key_cycle_attempts if a.get("error_type") == "SocketTimeout"]
                    connerr_keys = [f"k{a.get('key_idx',a.get('nv_key_idx',0))+1}" for a in result.key_cycle_attempts if a.get("error_type") in ("ConnectionRefusedError", "ConnectionError", "NVConnectionRefusedError", "NVConnectionError")]
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
                    return

                client_status = get_upstream_status_for_client(resp_status)
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

    # ─── Anthropic-format /v1/models endpoints ───
    # R35: Only expose ONE model (claude-opus-4-8) to CC. No manual model switching.
    # CC always sends claude-opus-4-8 → dispatcher auto-fallback handles routing.
    # MODEL_MAP still accepts any model name in requests (backward compat),
    # but /v1/models listing only shows the single canonical model.
    CC_FRONTEND_MODEL = "claude-opus-4-8"

    def _anthropic_models_list(self):
        """Return Anthropic-format model list — only claude-opus-4-8 (R35)."""
        mapped = MODEL_MAP.get(self.CC_FRONTEND_MODEL, DEFAULT_MODEL)
        safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, DEFAULT_CONTEXT_FALLBACK)
        self._send_json(200, {
            "data": [{
                "id": self.CC_FRONTEND_MODEL,
                "type": "model",
                "display_name": "Claude Opus 4",
                "created_at": "2024-01-01T00:00:00Z",
                "context_window": safety,
            }],
            "has_more": False,
        })

    def _anthropic_model_detail(self, model_id):
        """Return Anthropic-format model detail — maps any ID to the same underlying model."""
        mapped = MODEL_MAP.get(model_id, DEFAULT_MODEL)
        safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, DEFAULT_CONTEXT_FALLBACK)
        self._send_json(200, {
            "id": model_id,
            "type": "model",
            "display_name": "Claude Opus 4",
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
