#!/usr/bin/env python3
"""Configuration for ms-gateway — ModelScope API direct gateway.

Replaces LiteLLM ms_uni41001 (70 dep, ~1.5GB image) with a lightweight
Python gateway (~50MB) that does the same thing: map model_name →
MS variant ID + MS API key, then forward to ModelScope via HTTPS.

No routing, no retries, no cooldown, no DB — just a mapping table + forward.
The upstream proxies (cc-proxy, codex-proxy, passthrough-proxy) handle all
v×k round-robin, 429 key cycling, and format conversion themselves.
"""
import os
import re
import json
import ssl
import threading
import time

# ─── Network ──────────────────────────────────────────────────────────────
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "4000"))
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "300"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "300"))

# ─── ModelScope API ──────────────────────────────────────────────────────
MS_BASEURL = os.environ.get("MS_BASEURL", "https://api-inference.modelscope.cn/v1")
MS_KEYS = []
for i in range(1, 8):
    key = os.environ.get(f"MS_KEY{i}", "")
    if key:
        MS_KEYS.append(key)
NUM_KEYS = len(MS_KEYS)

# Auth key for incoming requests (from cc-proxy etc.)
GATEWAY_KEY = os.environ.get("GATEWAY_KEY", os.environ.get("LITELLM_MASTER_KEY", "sk-litellm-local"))

# ─── Variant model IDs — NEVER change (each has independent 200/id/day quota) ───
# These are the 10 case-variant model IDs on ModelScope that all resolve to GLM-5.2.
MS_VARIANT_IDS = [
    "ZHIPUAI/GLM-5.2",      # v1
    "ZHIPUAI/GLm-5.2",      # v2
    "ZHIPUAI/GlM-5.2",      # v3
    "ZHIPUAI/Glm-5.2",      # v4
    "ZHIPUAI/gLM-5.2",      # v5
    "ZHIPUAI/gLm-5.2",      # v6
    "ZHIPUAI/glM-5.2",      # v7
    "ZHIPUAI/glm-5.2",      # v8
    "ZHIPUAi/GLM-5.2",      # v9
    "ZHIPUAi/GLm-5.2",      # v10
]
NUM_VARIANTS = len(MS_VARIANT_IDS)

# ─── Logging ──────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# ─── Model name → MS variant ID + key mapping ──────────────────────────────
# model_name format: "glm5.1v{V}k{K}" where V=1-10, K=1-7
# Resolves to: MS_VARIANT_IDS[V-1] + MS_KEYS[K-1]
# Also supports bare model names for backward compat.

def resolve_model(model_name):
    """Resolve frontend model_name to (ms_variant_id, ms_api_key, display_name).

    Args:
        model_name: e.g. "glm5.1v3k5", "glm5.1", "claude-opus-4-8"

    Returns:
        (variant_id, api_key, display_name) tuple
        variant_id: MS model ID like "ZHIPUAI/GlM-5.2"
        api_key: MS API key like "ms-bb4e5bee..."
        display_name: for logging, e.g. "glm5.1v3k5→ZHIPUAI/GlM-5.2:k5"

    Raises ValueError if model_name cannot be resolved.
    """
    # Exact v×k format: "glm5.1v{V}k{K}"
    match = re.match(r"^glm5\.1v(\d+)k(\d+)$", model_name)
    if match:
        v = int(match.group(1))
        k = int(match.group(2))
        if v < 1 or v > NUM_VARIANTS:
            raise ValueError(f"variant index {v} out of range (1-{NUM_VARIANTS})")
        if k < 1 or k > NUM_KEYS:
            raise ValueError(f"key index {k} out of range (1-{NUM_KEYS})")
        variant_id = MS_VARIANT_IDS[v - 1]
        api_key = MS_KEYS[k - 1]
        display = f"{model_name}→{variant_id}:k{k}"
        return (variant_id, api_key, display)

    # Backward compat: bare names → default to v1k1
    compat_names = {
        "glm5.1": ("ZHIPUAI/GLM-5.2", 1, 1),
        "glm-5.2": ("ZHIPUAI/GLM-5.2", 1, 1),
        "zhipuai/glm-5.2": ("ZHIPUAI/GLM-5.2", 1, 1),
    }
    if model_name in compat_names:
        vid, v, k = compat_names[model_name]
        return (vid, MS_KEYS[k - 1], f"{model_name}→{vid}:k{k}")

    # Claude aliases → v1k1
    claude_aliases = [
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4",
        "claude-sonnet-4-6", "claude-sonnet-4", "claude-haiku-4-5",
        "claude-sonnet-4-20250514", "claude-sonnet-4-6-20250514",
        "claude-opus-4-20250514", "claude-opus-4-8-20250514",
        "claude-haiku-4-5-20251001",
    ]
    if model_name in claude_aliases:
        return (MS_VARIANT_IDS[0], MS_KEYS[0], f"{model_name}→{MS_VARIANT_IDS[0]}:k1")

    # OpenAI aliases → v1k1
    openai_aliases = ["gpt-4o", "gpt-4o-mini", "o3", "o3-mini", "o4-mini",
                      "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"]
    if model_name in openai_aliases:
        return (MS_VARIANT_IDS[0], MS_KEYS[0], f"{model_name}→{MS_VARIANT_IDS[0]}:k1")

    raise ValueError(f"unknown model_name: {model_name}")


def build_model_list():
    """Build the /v1/models response — list all 70 model_name entries."""
    models = []
    for v in range(1, NUM_VARIANTS + 1):
        for k in range(1, NUM_KEYS + 1):
            name = f"glm5.1v{v}k{k}"
            models.append({
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "zhipuai",
                "context_length": 131072,
                "max_tokens": 131072,
            })
    # Backward compat entries
    for compat_name in ["glm5.1", "glm-5.2", "claude-opus-4-8"]:
        models.append({
            "id": compat_name,
            "object": "model",
            "created": 1700000000,
            "owned_by": "zhipuai",
            "context_length": 131072,
            "max_tokens": 131072,
        })
    return models


# ─── SSL context ──────────────────────────────────────────────────────────
_ssl_context = ssl.create_default_context()

# ─── Logging helpers ──────────────────────────────────────────────────────
_log_lock = threading.Lock()

def _log(tag, msg):
    """Simple stderr logging with timestamp."""
    ts = time.strftime("%H:%M:%S", time.localtime())
    with _log_lock:
        print(f"[{ts}] [{tag}] {msg}", flush=True)
