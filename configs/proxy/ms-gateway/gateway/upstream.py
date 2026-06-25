#!/usr/bin/env python3
"""ModelScope API direct HTTPS call — the core of ms-gateway.

Replaces LiteLLM's OpenAI SDK + Flask routing with a direct
http.client.HTTPSConnection call to ModelScope API.

Much simpler than hm-proxy's NV path (no SOCKS5, no SSL tunnel hack,
no pexec function IDs, no per-key proxy) — just straight HTTPS to MS.

R44: stream_passthrough_chunked — writes SSE data in HTTP chunked encoding
format so cc-proxy's HTTP/1.1 client can read incrementally. Previous
stream_passthrough wrote raw data which worked with HTTP/1.0 Connection:close
but broke cc-proxy's incremental SSE reading.
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


def stream_passthrough_chunked(resp, conn, wfile, display_name):
    """Stream ModelScope SSE response in HTTP chunked transfer encoding.

    R44: cc-proxy uses HTTPConnection (HTTP/1.1 client) to read from ms-gateway.
    When ms-gateway responded with HTTP/1.0 + Connection:close, cc-proxy's
    resp.read(8192) couldn't read SSE data incrementally — it blocked until
    the connection closed, then either got all data at once or IncompleteRead(0).

    With HTTP/1.1 + Transfer-Encoding:chunked:
      - Each SSE chunk from MS is wrapped as a HTTP chunk: size\r\n data\r\n
      - cc-proxy's resp.read(8192) reads each HTTP chunk incrementally
      - Final chunk: 0\r\n\r\n signals end of stream
      - No Content-Length needed (stream length unknown upfront)

    Args:
        resp: http.client.HTTPResponse from MS
        conn: the HTTPSConnection (for cleanup after stream ends)
        wfile: client wfile to write to (must send chunked encoding)
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
            # Write as HTTP chunked encoding: hex_size\r\n data\r\n
            chunk_size = len(chunk)
            wfile.write(f"{chunk_size:x}\r\n".encode("ascii"))
            wfile.write(chunk)
            wfile.write(b"\r\n")
            wfile.flush()  # Flush each chunk to ensure cc-proxy can read incrementally
            bytes_sent += chunk_size
        # End of chunked stream: 0\r\n\r\n
        wfile.write(b"0\r\n\r\n")
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
