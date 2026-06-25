#!/usr/bin/env python3
"""ms-gateway HTTP handler — OpenAI chat/completions format.

Receives POST /v1/chat/completions requests from upstream proxies (cc-proxy,
codex-proxy, passthrough-proxy), resolves model_name → MS variant ID + key,
and forwards directly to ModelScope API via HTTPS.

This is a pure pass-through gateway — no routing, no retries, no cooldown,
no format conversion. The upstream proxies handle all the intelligence.
"""
import http.server
import json
import time
import urllib.parse

from .config import (
    LISTEN_PORT, GATEWAY_KEY, resolve_model, build_model_list, _log
)
from .upstream import call_modelscope, stream_passthrough, collect_response


class MsGatewayHandler(http.server.BaseHTTPRequestHandler):
    """Handle requests to ms-gateway."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/"):
            self._send_json(200, {
                "status": "ok",
                "service": "ms-gateway",
                "port": LISTEN_PORT,
                "num_models": 70,
            })
        elif parsed.path in ("/v1/models", "/models"):
            self._send_json(200, {"object": "list", "data": build_model_list()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/", "/v1/models", "/models"):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/chat/completions":
            self._handle_chat_completions()
        else:
            self._send_json(404, {"error": f"ms-gateway only serves /v1/chat/completions"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ─── POST /v1/chat/completions ──────────────────────────────────
    def _handle_chat_completions(self):
        """Forward chat/completions request to ModelScope API.

        Flow:
          1. Read request body
          2. Resolve model_name → (variant_id, api_key, display_name)
          3. Replace body.model with variant_id
          4. Forward to ModelScope via HTTPS
          5. On success: return response (stream or non-stream)
          6. On error: return error response
        """
        t_start = time.time()

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, {"error": "empty request body"})
            return

        try:
            raw_body = self.rfile.read(content_length)
            oai_body = json.loads(raw_body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"invalid JSON: {e}"})
            return

        model_name = oai_body.get("model", "")
        is_stream = oai_body.get("stream", False)

        # Resolve model_name → MS variant ID + API key
        try:
            variant_id, api_key, display_name = resolve_model(model_name)
        except ValueError as e:
            self._send_json(400, {"error": {"message": str(e), "type": "invalid_request_error"}})
            return

        # Call ModelScope API
        result = call_modelscope(oai_body, variant_id, api_key, display_name, is_stream)

        if isinstance(result, tuple) and len(result) == 2:
            status_or_resp, second = result

            # Check if it's a success (resp, conn) or error (status, error_dict)
            if hasattr(status_or_resp, 'read'):
                # Success: (resp, conn)
                resp = status_or_resp
                conn = second

                if is_stream:
                    # Streaming: pass through SSE chunks directly
                    self.send_response(200)
                    # Forward MS response headers
                    for hdr in ("Content-Type", "Transfer-Encoding"):
                        val = resp.getheader(hdr)
                        if val:
                            self.send_header(hdr, val)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    stream_passthrough(resp, conn, self.wfile, display_name)
                else:
                    # Non-streaming: collect full response and send
                    body = collect_response(resp, conn, display_name)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    # Forward MS quota headers if present
                    for hdr_key in (
                        "llm_provider-modelscope-ratelimit-model-requests-remaining",
                        "llm_provider-modelscope-ratelimit-requests-remaining",
                    ):
                        val = resp.getheader(hdr_key)
                        if val:
                            self.send_header(hdr_key, val)
                    self.end_headers()
                    self.wfile.write(body)
            else:
                # Error: (status_code, error_dict)
                error_status = status_or_resp
                error_json = second
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log("MS-FAIL", f"{display_name} → {error_status} after {elapsed_ms}ms")

                # Forward error as OpenAI-format error response
                # (upstream proxies will convert to Anthropic/OpenAI/Responses format)
                error_response = {
                    "error": {
                        "message": error_json.get("error", str(error_json))[:500] if isinstance(error_json, dict) else str(error_json)[:500],
                        "type": "upstream_error",
                        "code": str(error_status),
                    }
                }
                if isinstance(error_json, dict) and "error" in error_json:
                    # Preserve MS error structure if available
                    ms_error = error_json["error"]
                    if isinstance(ms_error, dict):
                        error_response["error"]["message"] = ms_error.get("message", str(ms_error))[:500]
                        error_response["error"]["type"] = ms_error.get("type", "upstream_error")

                # Add retry-after header for 429 (to control CC retry behavior)
                extra_headers = {}
                if error_status == 429:
                    # Check if it's quota exhaustion or transient
                    err_str = json.dumps(error_json)
                    if "配额" in err_str or "quota" in err_str.lower() or "超出" in err_str:
                        extra_headers["retry-after"] = "180"  # >60s → CC gives up
                        _log("MS-429-QUOTA", f"{display_name} quota exhaustion → retry-after:180")
                    else:
                        extra_headers["retry-after"] = "5"  # transient burst → CC waits 5s
                        _log("MS-429-BURST", f"{display_name} transient 429 → retry-after:5")

                self._send_json(error_status, error_response, extra_headers)

    # ─── Helpers ──────────────────────────────────────────────────────────
    def _send_json(self, code, data, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default HTTPServer logging — we use our own _log."""
        pass
