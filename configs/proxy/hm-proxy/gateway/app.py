#!/usr/bin/env python3
"""Hermes NV proxy entry point — ThreadedHTTPServer startup."""
import os
import sys
from http.server import ThreadingHTTPServer

from gateway.config import LISTEN_HOST, LISTEN_PORT, PROXY_ROLE, NV_NUM_KEYS, NV_ENABLED
from gateway.handlers import ProxyHandler


def create_and_start_server():
    print(f"[HM-PROXY] Starting Hermes NV proxy on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr, flush=True)
    print(f"[HM-PROXY] PROXY_ROLE={PROXY_ROLE} NV_NUM_KEYS={NV_NUM_KEYS} NV_ENABLED={NV_ENABLED}", file=sys.stderr, flush=True)

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(f"[HM-PROXY] Listening on {LISTEN_HOST}:{LISTEN_PORT} (role={PROXY_ROLE})", file=sys.stderr, flush=True)
    server.serve_forever()
