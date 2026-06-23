#!/usr/bin/env python3
"""Gateway proxy entry point.

R29: Three proxy containers, each with a different role:
  40001 (cc):          CC → Anthropic format → glm5.1 v×k cycling
  40002 (codex):       Codex → Responses API → glm5.1 v×k cycling
  40003 (passthrough): _ol/_oc/_hm_ms → OpenAI passthrough → glm5.1 v×k cycling

All three share the same gateway code (same Docker image).
Difference is injected via PROXY_ROLE env var + different upstream model.

Env vars: see config.py for full list.
"""
import socketserver

from .config import LISTEN_HOST, LISTEN_PORT, MODEL_UPSTREAMS, UPSTREAM_TIMEOUT, PROXY_ROLE, DEFAULT_UPSTREAM_MODEL
from .logger import _log


class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    from .handlers import ProxyHandler
    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    _log("START", f"Proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    _log("START", f"PROXY_ROLE={PROXY_ROLE} — serving {DEFAULT_UPSTREAM_MODEL} upstream")
    _log("START", f"UPSTREAM_TIMEOUT={UPSTREAM_TIMEOUT}s (per-key HTTP timeout)")

    for model_key, upstream in MODEL_UPSTREAMS.items():
        _log("START", f"  {model_key} gateway: {upstream['chat_url']}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("STOP", "Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
