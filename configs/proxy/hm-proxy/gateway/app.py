#!/usr/bin/env python3
"""Hermes NV proxy entry point — ThreadedHTTPServer startup — R38.6."""
import os
import sys
from http.server import ThreadingHTTPServer

from gateway.config import (
    LISTEN_HOST, LISTEN_PORT, PROXY_ROLE,
    HM_NUM_KEYS, HM_LITELLM_URLS,
    NV_MODEL_TIERS, DEFAULT_NV_MODEL,
)
from gateway.handlers import ProxyHandler


def create_and_start_server():
    print(f"[HM-PROXY] Starting Hermes NV proxy on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr, flush=True)
    print(f"[HM-PROXY] PROXY_ROLE={PROXY_ROLE} HM_NUM_KEYS={HM_NUM_KEYS} "
           f"LiteLLM_urls={len(HM_LITELLM_URLS)} "
           f"tiers={NV_MODEL_TIERS} default={DEFAULT_NV_MODEL}", file=sys.stderr, flush=True)

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(f"[HM-PROXY] Listening on {LISTEN_HOST}:{LISTEN_PORT} "
           f"(role={PROXY_ROLE}, default_tier={DEFAULT_NV_MODEL}, "
           f"fallback_chain={NV_MODEL_TIERS})", file=sys.stderr, flush=True)
    server.serve_forever()
