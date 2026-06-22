# R35: CC dispatcher — pure auto-fallback relay.
#
# CC sends ONE model (claude-opus-4-8 or glm5.1_cc) to ONE base URL (:40000).
# This dispatcher always tries the PRIMARY (40005) first; on connection failure,
# automatically falls through to the FALLBACK (40001). CC never knows which
# backend actually served — zero-configuration, zero manual switching.
#
# No model-based routing. No parsing. Pure byte-level relay with one fallback rule.
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
from urllib.parse import urlsplit

PRIMARY = os.environ.get("DISPATCH_PRIMARY", "http://auth_to_api_40005:40005")
FALLBACK = os.environ.get("DISPATCH_FALLBACK", "http://auth_to_api_40001:40001")
PORT = int(os.environ.get("LISTEN_PORT", "40000"))
CONNECT_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host",
       "accept-encoding"}  # keep content-length so the relay can signal body end


def parse(url):
    p = urlsplit(url)
    return p.hostname, p.port or 80


def _try_relay(self, upstream_url, role_label, body):
    """Try to relay request to one upstream.
    Returns True on success (response fully streamed to client),
    False on connection failure (no data sent to client, try other upstream).
    """
    host, port = parse(upstream_url)
    conn = HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
    try:
        conn.request(self.command, self.path, body=body, headers=self._hdrs())
        resp = conn.getresponse()
    except Exception as e:
        err_class = type(e).__name__
        sys.stderr.write(f"[DISPATCH] {role_label}({upstream_url}) connect failed: "
                         f"{err_class}: {e}\n")
        try:
            conn.close()
        except Exception:
            pass
        return False

    # Stream response to client
    self.send_response(resp.status)
    for k, v in resp.getheaders():
        if k.lower() in HOP:
            continue
        self.send_header(k, v)
    self.close_connection = True
    self.send_header("Connection", "close")
    self.end_headers()

    try:
        while not resp.closed:
            chunk = resp.read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
    except Exception as e:
        sys.stderr.write(f"[DISPATCH] stream relay ended: {e}\n")
    finally:
        conn.close()
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relay(self):
        body = getattr(self, "_body", b"") or b""

        # Always try primary first; on failure → fallback
        success = _try_relay(self, PRIMARY, "primary", body)
        if not success:
            sys.stderr.write(f"[DISPATCH] primary failed → auto-fallback to {FALLBACK}\n")
            success_fb = _try_relay(self, FALLBACK, "fallback", body)
            if not success_fb:
                sys.stderr.write(f"[DISPATCH] BOTH upstreams failed\n")
                self._send_err(502, "dispatcher: both upstreams unavailable")

    def _hdrs(self):
        out = {}
        for k, v in self.headers.items():
            if k.lower() in HOP:
                continue
            out[k] = v
        body_len = len(getattr(self, "_body", b"") or b"")
        if body_len:
            out["Content-Length"] = str(body_len)
        return out

    def _send_err(self, code, message):
        # R35.6+: Set close_connection=True and send Connection: close header
        # to prevent client from reusing a dead connection after error response.
        # Without this, HTTP/1.1 default keep-alive lets the client send another
        # request on the same socket — which will also fail, creating error cascades.
        self.close_connection = True
        err = json.dumps({"type": "error", "error": {"type": "api_error",
                          "message": message}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(err)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(err)

    def do_GET(self):
        if self.path == "/health":
            payload = json.dumps({
                "status": "ok",
                "role": "dispatcher",
                "primary": PRIMARY,
                "fallback": FALLBACK,
                "model": "claude-opus-4-8",
                "auto_fallback": "enabled",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._body = b""
        self._relay()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        self._body = self.rfile.read(length) if length > 0 else b""
        self._relay()

    def log_message(self, fmt, *args):
        pass  # Suppress default — we log via stderr above


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[DISPATCH] listening :{PORT} "
                     f"primary={PRIMARY} fallback={FALLBACK} "
                     f"auto_fallback=enabled model=claude-opus-4-8\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
