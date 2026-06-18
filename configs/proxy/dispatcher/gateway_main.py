# R31.7: CC model→endpoint dispatcher.
# CC selects model via /model (claude-opus-4-8 ↔ claude-sonnet-4-6); both go to the
# same ANTHROPIC_BASE_URL. This dispatcher listens ONE port and routes by request
# body model field, so /model switching = upstream switching, no CC restart needed.
#
# Routing:
#   claude-sonnet-*  → 40001 (fallback)
#   everything else → 40005 (primary, incl opus + unknown + glm5.2_*)
#
# Pure HTTP relay: no parsing/truncation/format conversion. Uses http.client for
# raw chunked streaming (urllib blocks on SSE responses with no Content-Length).
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
from urllib.parse import urlsplit

PRIMARY = os.environ.get("DISPATCH_PRIMARY", "http://127.0.0.1:40005")
FALLBACK = os.environ.get("DISPATCH_FALLBACK", "http://127.0.0.1:40001")
PORT = int(os.environ.get("LISTEN_PORT", "40000"))
CONNECT_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))

SONNET_MODELS = {"claude-sonnet-4-6", "claude-sonnet-4-5", "claude-3-7-sonnet"}

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host",
       "accept-encoding"}  # keep content-length so the relay can signal body end to the client


def pick_upstream(model: str) -> str:
    m = (model or "").strip()
    if m in SONNET_MODELS or "sonnet" in m:
        return FALLBACK
    return PRIMARY


def parse(url: str):
    p = urlsplit(url)
    return p.hostname, p.port or 80


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relay(self):
        body = getattr(self, "_body", b"") or b""
        model = ""
        if body:
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    model = obj.get("model", "") or ""
            except Exception:
                pass

        upstream = pick_upstream(model)
        host, port = parse(upstream)

        conn = HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
        try:
            conn.request(self.command, self.path, body=body, headers=self._hdrs())
            resp = conn.getresponse()
        except Exception as e:
            sys.stderr.write(f"[DISPATCH] upstream connect failed: {upstream} model={model} err={e}\n")
            self._send_err(502, f"dispatcher: upstream {upstream} unavailable")
            try:
                conn.close()
            except Exception:
                pass
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP:
                continue
            self.send_header(k, v)
        # Force close on this connection so the client (CC) can always detect body end,
        # whether upstream used Content-Length, chunked, or plain close-delimited body.
        self.close_connection = True
        self.send_header("Connection", "close")
        self.end_headers()

        # Stream raw chunks straight through the socket.
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

    def _hdrs(self):
        out = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in HOP:
                continue
            out[k] = v
        body_len = len(getattr(self, "_body", b"") or b"")
        if body_len:
            out["Content-Length"] = str(body_len)
        return out

    def _send_err(self, code, message):
        err = json.dumps({"type": "error", "error": {"type": "api_error",
                          "message": message}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(err)))
        self.end_headers()
        self.wfile.write(err)

    def do_GET(self):
        if self.path == "/health":
            payload = b'{"status":"ok","role":"dispatcher","primary":"' + PRIMARY.encode() + \
                      b'","fallback":"' + FALLBACK.encode() + b'"}'
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
        sys.stderr.write(f"[DISPATCH] {self.address_string()} {fmt % args}\n")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[DISPATCH] listening :{PORT} primary={PRIMARY} fallback={FALLBACK}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
