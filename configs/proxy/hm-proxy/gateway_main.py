#!/usr/bin/env python3
"""Hermes专用 NV proxy gateway — R37.

hm40006 只服务 Hermes agent，只走 NV API (5 key 顺序循环)。
链路: Hermes → 40006 → NV API (per-key mihomo proxy, 7894-7899) → NVIDIA integrate API

关键设计：
  - NV-only routing（无 MS interleaving）
  - 顺序循环 k1→k2→k3→k4→k5→k1...（持久化计数器，断电不归零）
  - Per-key proxy URL（NV_PROXY_URL_MAP）
  - OpenAI passthrough format（Hermes 用 /v1/chat/completions）
  - MSG-FIX: messages 以 assistant 结尾 → 自动追加 user "Continue."
  - NV unsupported params strip (thinking_budget/reasoning_effort/stream_options)
  - sock.settimeout() after conn.request() (R36.2 read timeout fix)
"""
from gateway.app import create_and_start_server

if __name__ == "__main__":
    create_and_start_server()
