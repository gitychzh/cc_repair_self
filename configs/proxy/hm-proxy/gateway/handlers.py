#!/usr/bin/env python3
"""HTTP handler for Hermes NV proxy — R37.

Only serves /v1/chat/completions (OpenAI format, Hermes agent).
NV-only routing — no MS (ModelScope) interleaving.
MSG-FIX: messages ending with assistant → append user "Continue."
NV unsupported params strip.
"""
import http.server
import json
import os
import time
import datetime
import uuid
import http.client
import socket
import urllib.parse

from .config import (
    NV_NUM_KEYS, NV_ENABLED,
    NV_MODEL_IDS, DEFAULT_NV_MODEL, MODEL_MAP, detect_nv_model,
    PROXY_ROLE, LISTEN_PORT,
    MODEL_INPUT_TOKEN_SAFETY, DEFAULT_CONTEXT_FALLBACK,
)
from .logger import _log, _log_metrics, _log_error_detail
from .upstream import execute_nv_request, UpstreamResult
from .error_mapping import format_nv_all_keys_exhausted, format_nv_error_upstream


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/"):
            self._send_json(200, {
                "status": "ok",
                "proxy_role": PROXY_ROLE,
                "nv_enabled": NV_ENABLED,
                "nv_num_keys": NV_NUM_KEYS,
                "port": LISTEN_PORT,
            })
        elif parsed.path in ("/v1/models", "/models"):
            self._proxy_models()
        else:
            self._send_json(404, {"error": {"message": "not found", "type": "invalid_request_error", "code": "404"}})

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/", "/v1/models", "/models", "/v1/chat/completions", "/chat/completions"):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        # Hermes proxy only serves /v1/chat/completions
        if parsed.path in ("/v1/chat/completions", "/chat/completions"):
            self._handle_openai_nv()
        else:
            self._send_json(404, {"error": {"message": f"Hermes proxy only serves /v1/chat/completions. Role={PROXY_ROLE}",
                                             "type": "invalid_request_error", "code": "404"}})

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ─── /v1/chat/completions — OpenAI format (Hermes / _hm) ───
    def _handle_openai_nv(self):
        """Handle OpenAI-format requests from Hermes agent.

        NV-only routing: 5 key sequential round-robin with persistent counter.
        MSG-FIX: if messages ends with assistant role, append user "Continue."
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
            "agent_type": "_hm",
            "stream": False,
            "total_input_chars": 0,
            "ttfb_ms": None,
            "duration_ms": 0,
            "status": 0,
            "error_type": None,
            "error_message": None,
            "upstream": "nv",
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

        request_model = body.get("model", DEFAULT_NV_MODEL)
        is_stream = body.get("stream", False)
        metrics["request_model"] = request_model
        metrics["stream"] = is_stream

        # Detect NV model from frontend model name
        mapped_model = detect_nv_model(request_model)
        nv_model_id = NV_MODEL_IDS.get(mapped_model, DEFAULT_NV_MODEL)
        metrics["mapped_model"] = mapped_model

        # Input chars estimation
        json_chars = len(json.dumps(body))
        metrics["total_input_chars"] = json_chars

        _log("REQ", f"model={request_model}→{mapped_model}→{nv_model_id} stream={is_stream} "
                    f"msgs={len(body.get('messages',[]))} agent=_hm")

        # ─── MSG-FIX (R35.10) ───
        messages = body.get("messages", [])
        original_msg_count = len(messages)
        if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
            body["messages"].append({"role": "user", "content": "Continue."})
            _log("MSG-FIX", f"appended user 'Continue.' to fix assistant-ending messages "
                           f"(original msgs={original_msg_count}, now {len(body['messages'])})")

        # Add stream_options.include_usage for streaming metrics
        if is_stream and "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}

        # ─── Execute NV-only request ───
        result = execute_nv_request(self, body, mapped_model, request_id, metrics, t_start)

        if not result.success:
            # ─── Error handling ───
            if result.all_keys_exhausted:
                metrics["status"] = 429 if result.all_429 else 502
                metrics["error_type"] = "all_nv_keys_exhausted"
                metrics["duration_ms"] = result.elapsed_ms
                metrics["key_cycle_attempts"] = len(result.key_cycle_attempts)
                metrics["key_cycle_details"] = result.key_cycle_attempts
                _log_metrics(metrics)

                error_payload, client_status = format_nv_all_keys_exhausted(result, mapped_model, request_model)
                extra_hdrs = None
                if client_status == 429:
                    extra_hdrs = {"retry-after": "5"}  # NV 429 is transient RPM, not quota
                self._send_json(client_status, error_payload, extra_headers=extra_hdrs)
                return
            else:
                # Non-cycling upstream error
                error_json = result.final_error_json
                resp_status = result.final_resp_status
                error_payload, client_status = format_nv_error_upstream(error_json, request_model, resp_status)
                extra_hdrs = None
                if client_status == 429:
                    extra_hdrs = {"retry-after": "5"}
                metrics["status"] = client_status
                metrics["error_type"] = "nv_upstream_error"
                metrics["error_message"] = str(error_json)[:200]
                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                _log_metrics(metrics)
                self._send_json(client_status, error_payload, extra_headers=extra_hdrs)
                return

        # ─── Success: pass through NV response ───
        resp = result.resp
        conn = result.conn
        metrics["nv_key_idx"] = result.nv_key_idx
        metrics["litellm_model"] = result.nv_model_label
        if result.key_cycle_attempts:
            metrics["key_cycle_429s_before_success"] = len(result.key_cycle_attempts)
            metrics["key_cycle_details"] = result.key_cycle_attempts

        if is_stream:
            self._stream_openai_passthrough(resp, conn, metrics, t_start, request_model)
        else:
            # Non-stream: read response, pass through as-is
            ttfb_start = time.time()
            resp_body = resp.read()
            metrics["status"] = 200
            metrics["duration_ms"] = int((time.time() - t_start) * 1000)
            metrics["ttfb_ms"] = int((ttfb_start - t_start) * 1000)

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
        """Pass through OpenAI streaming SSE response directly to Hermes."""
        ttfb_recorded = False
        streaming_input_tokens = 0
        streaming_output_tokens = 0
        sse_buffer = ""

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    # Process remaining buffer
                    remaining = sse_buffer.strip()
                    if remaining and remaining.startswith("data:") and remaining[5:].strip() != "[DONE]":
                        data_str = remaining[5:].strip()
                        if data_str:
                            try:
                                data = json.loads(data_str)
                                fr = data.get("choices", [{}])[0].get("finish_reason")
                                if fr:
                                    metrics["finish_reason"] = fr
                                chunk_usage = data.get("usage", {})
                                if chunk_usage:
                                    pt = chunk_usage.get("prompt_tokens", 0)
                                    ct = chunk_usage.get("completion_tokens", 0)
                                    if pt > 0:
                                        streaming_input_tokens = pt
                                    if ct > 0:
                                        streaming_output_tokens = ct
                            except Exception:
                                pass
                    break

                if not ttfb_recorded:
                    metrics["ttfb_ms"] = int((time.time() - t_start) * 1000)
                    ttfb_recorded = True

                sse_buffer += chunk.decode("utf-8", errors="replace")

                try:
                    while "\n" in sse_buffer:
                        line, sse_buffer = sse_buffer.split("\n", 1)
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]" or not data_str:
                            continue
                        try:
                            data = json.loads(data_str)
                            chunk_usage = data.get("usage", {})
                            if chunk_usage:
                                pt = chunk_usage.get("prompt_tokens", 0)
                                ct = chunk_usage.get("completion_tokens", 0)
                                if pt > 0:
                                    streaming_input_tokens = pt
                                if ct > 0:
                                    streaming_output_tokens = ct
                            fr = data.get("choices", [{}])[0].get("finish_reason")
                            if fr:
                                metrics["finish_reason"] = fr
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    pass

                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except Exception:
                    break

        except (http.client.RemoteDisconnected, ConnectionResetError,
                OSError, http.client.IncompleteRead, socket.timeout) as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            error_class = type(e).__name__
            _log("ERR", f"NV stream {error_class} after {elapsed_ms}ms: {e}")
            metrics["error_type"] = f"NVStream_{error_class}"
        except Exception as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            error_class = type(e).__name__
            _log("ERR", f"NV stream unexpected {error_class} after {elapsed_ms}ms: {e}")
            metrics["error_type"] = f"NVStream_{error_class}"

        if metrics.get("error_type"):
            metrics["status"] = 502
        else:
            metrics["status"] = 200
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

    # ─── /v1/models ───
    def _proxy_models(self):
        """Return OpenAI-format model list for Hermes."""
        all_models = []
        for model_key, nv_model_id in NV_MODEL_IDS.items():
            context_len = MODEL_INPUT_TOKEN_SAFETY.get(model_key, DEFAULT_CONTEXT_FALLBACK)
            all_models.append({
                "id": model_key,
                "object": "model",
                "created": 0,
                "owned_by": "nvidia_hermes",
                "context_length": context_len,
            })
        # Also include aliases from MODEL_MAP
        seen = set(NV_MODEL_IDS.keys())
        for alias, mapped in MODEL_MAP.items():
            if alias not in seen and mapped in NV_MODEL_IDS:
                seen.add(alias)
                context_len = MODEL_INPUT_TOKEN_SAFETY.get(mapped, DEFAULT_CONTEXT_FALLBACK)
                all_models.append({
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "nvidia_hermes_alias",
                    "context_length": context_len,
                })
        self._send_json(200, {"object": "list", "data": all_models})

    # ─── Helpers ───
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

    def log_message(self, fmt, *args):
        pass  # Suppress default logging
