#!/usr/bin/env python3
"""ms-gateway entry point.

Lightweight ModelScope API gateway replacing LiteLLM 41001.
Receives OpenAI chat/completions, resolves model_name → MS variant + key,
 forwards to ModelScope via direct HTTPS call.
"""
import socketserver

from gateway.config import LISTEN_HOST, LISTEN_PORT, MS_KEYS, MS_VARIANT_IDS, NUM_KEYS, NUM_VARIANTS, MS_BASEURL, _log
from gateway.handler import MsGatewayHandler


class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), MsGatewayHandler)
    _log("START", f"ms-gateway listening on {LISTEN_HOST}:{LISTEN_PORT}")
    _log("START", f"{NUM_VARIANTS} variants × {NUM_KEYS} keys = {NUM_VARIANTS * NUM_KEYS} models")
    _log("START", f"MS API: {MS_BASEURL}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("STOP", "Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
