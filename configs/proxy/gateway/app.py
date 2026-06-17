#!/usr/bin/env python3
"""Gateway proxy entry point.

Architecture:
  CC/OL/OC/HM/CX(40001) → this proxy (format conversion + metrics + variant×key 2D round-robin)
      → 41001 ms_uni41001 LiteLLM (glm5.2 only, 70 dep)

Env vars: see config.py for full list.
"""
import socketserver

from .config import LISTEN_HOST, LISTEN_PORT, MODEL_UPSTREAMS, UPSTREAM_TIMEOUT
from .logger import _log
from .handlers import ProxyHandler


class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    _log("START", f"Proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    _log("START", f"UPSTREAM_TIMEOUT={UPSTREAM_TIMEOUT}s (per-key HTTP timeout)")
    _log("START", f"GLM-5.2 primary gateway: {MODEL_UPSTREAMS['glm5.2']['chat_url']}")
    fb_url = MODEL_UPSTREAMS['glm5.2'].get('fallback_chat_url', '')
    if fb_url:
        _log("START", f"GLM-5.2 fallback gateway: {fb_url}")
    else:
        _log("START", f"GLM-5.2 fallback gateway: not configured")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("STOP", "Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()