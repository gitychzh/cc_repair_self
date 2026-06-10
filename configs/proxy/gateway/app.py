#!/usr/bin/env python3
"""Gateway proxy entry point.

Architecture:
  CC(40001) → this proxy (format conversion + metrics + input safety)
      → 41001 LiteLLM (glm5.1, with retry/fallback/routing)
      → 42001 LiteLLM (dsv4p, with retry/fallback/routing)

Env vars: see config.py for full list.
"""
import socketserver

from .config import LISTEN_HOST, LISTEN_PORT, MODEL_UPSTREAMS
from .logger import _log
from .handlers import ProxyHandler


class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    _log("START", f"Proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    _log("START", f"GLM-5.1 gateway: {MODEL_UPSTREAMS['glm5.1']['chat_url']}")
    _log("START", f"DSv4P gateway: {MODEL_UPSTREAMS['dsv4p']['chat_url']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("STOP", "Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()