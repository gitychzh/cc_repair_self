#!/usr/bin/env python3
"""ModelScope API direct HTTPS call — the core of ms-gateway.

Replaces LiteLLM's OpenAI SDK + Flask routing with a direct
http.client.HTTPSConnection call to ModelScope API.

Much simpler than hm-proxy's NV path (no SOCKS5, no SSL tunnel hack,
no pexec function IDs, no per-key proxy) — just straight HTTPS to MS.
"""
import json
import http.client
import ssl
import urllib.parse
import time

from .config import (
    MS_BASEURL, UPSTREAM_TIMEOUT, _ssl_context, _log
)


def call_modelscope(oai_body, variant_id, api_key, display_name, is_stream=False):
    """Call ModelScope API directly via HTTPS.

    Args:
        oai_body: dict — OpenAI chat/completions request body
        variant_id: str — MS model ID (e.g. "ZHIPUAI/GlM-5.2")
        api_key: str — MS API key
        display_name: str — for logging
        is_stream: bool — whether streaming

    Returns:
        (resp, conn) on success (resp is http.client.HTTPResponse)
        OR (error_status, error_body_dict) on failure
    """
    # Replace model name with real MS variant ID
    body = dict(oai_body)
    body["model"] = variant_id

    # MSG-FIX: messages ending with assistant role → append user "Continue."
    # GLM API rejects sequences ending with assistant.
    messages = body.get("messages", [])
    if messages and messages[-1].get("role") == "assistant":
        messages.append({"role": "user", "content": "Continue."})
        body["messages"] = messages

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Length": str(len(data)),
    }

    parsed = urllib.parse.urlparse(MS_BASEURL)
    ms_host = parsed.hostname
    ms_port = parsed.port or 443
    ms_path = (parsed.path.rstrip("/") or "/v1") + "/chat/completions"

    _log("MS-CALL", f"{display_name} → POST https://{ms_host}{ms_path} stream={is_stream}")

    try:
        conn = http.client.HTTPSConnection(
            ms_host, ms_port,
            timeout=UPSTREAM_TIMEOUT,
            context=_ssl_context,
        )
        conn.request("POST", ms_path, body=data, headers=headers)
        resp = conn.getresponse()

        if resp.status < 400:
            _log("MS-OK", f"{display_name} → {resp.status} (stream={is_stream})")
            return (resp, conn)
        else:
            # Error — read body and return
            error_body = resp.read()
            try:
                error_json = json.loads(error_body)
            except Exception:
                error_json = {"error": error_body.decode("utf-8", errors="replace")}
            conn.close()
            _log("MS-ERR", f"{display_name} → {resp.status}: {json.dumps(error_json)[:200]}")
            return (resp.status, error_json)

    except Exception as e:
        error_class = type(e).__name__
        _log("MS-EXC", f"{display_name} → {error_class}: {e}")
        return (0, {"error": f"{error_class}: {str(e)[:200]}"})


def stream_passthrough(resp, conn, wfile, display_name):
    """Stream ModelScope SSE response directly to client wfile.

    Reads 8192-byte chunks from MS HTTPSConnection and writes them
    directly to the client's wfile — no parsing, no transformation,
    no buffering. Same pattern as hm-proxy.

    Args:
        resp: http.client.HTTPResponse from MS
        conn: the HTTPSConnection (for cleanup after stream ends)
        wfile: client wfile to write to
        display_name: for logging
    """
    try:
        ttfb = False
        bytes_sent = 0
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            if not ttfb:
                ttfb = True
                _log("MS-TTFB", f"{display_name} first chunk received")
            wfile.write(chunk)
            bytes_sent += len(chunk)
        wfile.flush()
        _log("MS-STREAM-END", f"{display_name} stream complete, {bytes_sent} bytes sent")
    except Exception as e:
        _log("MS-STREAM-ERR", f"{display_name} stream error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def collect_response(resp, conn, display_name):
    """Collect non-streaming ModelScope response and return body + close conn.

    Args:
        resp: http.client.HTTPResponse from MS
        conn: the HTTPSConnection (for cleanup)
        display_name: for logging

    Returns:
        bytes — the full response body
    """
    try:
        body = resp.read()
        _log("MS-COLLECT", f"{display_name} collected {len(body)} bytes")
        return body
    finally:
        try:
            conn.close()
        except Exception:
            pass
