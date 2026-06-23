#!/usr/bin/env python3
"""Configuration for Hermes NV proxy (hm40006).

All configurable parameters are read from env vars with defaults.
This proxy is NV-only — no MS (ModelScope) routing.
Hermes agent uses OpenAI format (/v1/chat/completions).

R37: 5 NV keys in sequential round-robin with persistent counter.
Per-key proxy URL via NV_PROXY_URL_MAP for IP diversity.
"""
import os
import sys
import json
import time
import threading

# ─── Network ──────────────────────────────────────────────────────────────
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "40006"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "300"))
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "60"))

# ─── Proxy Role ────────────────────────────────────────────────────────────
# "passthrough" — serves /v1/chat/completions (OpenAI format)
PROXY_ROLE = os.environ.get("PROXY_ROLE", "passthrough")

# ─── Logging ──────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# ─── NV (NVIDIA) direct API tunnel ──────────────────────────────────────────
NV_BASEURL = os.environ.get("NV_BASEURL", "https://integrate.api.nvidia.com/v1")
NV_NUM_KEYS = int(os.environ.get("NV_NUM_KEYS", "5"))
NV_KEYS = []
for i in range(1, NV_NUM_KEYS + 1):
    key = os.environ.get(f"NV_KEY{i}", "")
    if key:
        NV_KEYS.append(key)
NV_TIMEOUT = int(os.environ.get("NV_TIMEOUT", "40"))
NV_ENABLED = bool(NV_BASEURL and NV_KEYS)

# Per-key proxy URL map — each NV key routes through a different mihomo port
# for IP diversity. Format: JSON {"0":"host.docker.internal:7894","1":"7895",...}
NV_PROXY_URL_MAP_RAW = os.environ.get("NV_PROXY_URL_MAP", "")
NV_PROXY_URL_MAP = {}
if NV_PROXY_URL_MAP_RAW:
    try:
        NV_PROXY_URL_MAP = json.loads(NV_PROXY_URL_MAP_RAW)
        # Ensure all values have http:// prefix
        for k, v in NV_PROXY_URL_MAP.items():
            if not v.startswith("http"):
                NV_PROXY_URL_MAP[k] = f"http://{v}"
    except json.JSONDecodeError:
        print(f"[HM-CONFIG] WARN: NV_PROXY_URL_MAP parse error: {NV_PROXY_URL_MAP_RAW}", file=sys.stderr, flush=True)

NV_PROXY_URL = os.environ.get("NV_PROXY_URL", "")  # Fallback single proxy

# ─── NV model IDs on NVIDIA API ──────────────────────────────────────────────
# 4 models available on NV integrate API, Hermes can choose by model name suffix
NV_MODEL_IDS = {
    "kimi_hm": "moonshotai/kimi-k2.6",
    "glm5.1_hm": "z-ai/glm-5.1",
    "minimax_hm": "minimaxai/minimax-m3",
    "deepseek_hm": "deepseek-ai/deepseek-v4-pro",
}

# Default model for Hermes (kimi first as requested)
DEFAULT_NV_MODEL = "moonshotai/kimi-k2.6"

# ─── Agent suffix for Hermes ──────────────────────────────────────────────
AGENT_SUFFIXES = {
    "_hm": {"name": "Hermes", "format": "openai"},
}
DEFAULT_AGENT_SUFFIX = "_hm"

# ─── Model name mapping ──────────────────────────────────────────────────
# Frontend model names → NV model IDs
MODEL_MAP = {
    # Hermes suffix models
    "kimi_hm": "kimi_hm",
    "glm5.1_hm": "glm5.1_hm",
    "minimax_hm": "minimax_hm",
    "deepseek_hm": "deepseek_hm",
    # Backward compat — no suffix = kimi (default for Hermes)
    "kimi": "kimi_hm",
    "kimi-k2.6": "kimi_hm",
    "moonshotai/kimi-k2.6": "kimi_hm",
    # GLM backward compat
    "glm5.1": "glm5.1_hm",
    "glm-5.1": "glm5.1_hm",
    # Other aliases
    "minimax": "minimax_hm",
    "minimax-m3": "minimax_hm",
    "deepseek": "deepseek_hm",
    "deepseek-v4-pro": "deepseek_hm",
}

def detect_nv_model(model_id: str) -> str:
    """Detect NV model ID from frontend model name.

    Returns: internal NV model key (kimi_hm/glm5.1_hm/minimax_hm/deepseek_hm)
    Falls back to DEFAULT_NV_MODEL.
    """
    mapped = MODEL_MAP.get(model_id, None)
    if mapped and mapped in NV_MODEL_IDS:
        return mapped
    return "kimi_hm"  # Default = kimi

# ─── Token estimation ──────────────────────────────────────────────────────
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("CHARS_PER_TOKEN_ESTIMATE", "3.0"))

# ─── Outbound throttle ──────────────────────────────────────────────────────
MIN_OUTBOUND_INTERVAL_S = float(os.environ.get("MIN_OUTBOUND_INTERVAL_S", "1.5"))
_outbound_last_sent = 0.0
_outbound_throttle_lock = threading.Lock()

def throttle_outbound():
    """Enforce MIN_OUTBOUND_INTERVAL_S between consecutive outbound requests."""
    if MIN_OUTBOUND_INTERVAL_S <= 0:
        return
    global _outbound_last_sent
    with _outbound_throttle_lock:
        now = time.monotonic()
        elapsed = now - _outbound_last_sent
        wait = MIN_OUTBOUND_INTERVAL_S - elapsed
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _outbound_last_sent = now

# ─── Persistent round-robin counter (R30/R31.3 pattern) ──────────────────
# Counter persists to rr_counter.json so container restarts/power loss
# do NOT reset the position. Every increment is immediately atomic persisted.
_RR_COUNTER_FILE = os.path.join(LOG_DIR, "rr_counter.json")
_vk_rr_counter = {}
_vk_rr_lock = threading.Lock()

def _load_rr_counter() -> None:
    """Restore counters from disk at startup."""
    try:
        with open(_RR_COUNTER_FILE, "r") as f:
            raw = f.read().strip()
        if not raw:
            return
        saved = json.loads(raw)
        if isinstance(saved, dict):
            for k, v in saved.items():
                if isinstance(k, str) and isinstance(v, int) and v >= 0:
                    _vk_rr_counter[k] = v
            print(f"[HM-RR] restored from {_RR_COUNTER_FILE}: {_vk_rr_counter}", file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[HM-RR] file corrupt ({e}); starting fresh", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[HM-RR] WARN could not load: {e}", file=sys.stderr, flush=True)

def _save_rr_counter() -> None:
    """Persist counters to disk atomically."""
    try:
        tmp = "%s.tmp.%d.%d" % (_RR_COUNTER_FILE, os.getpid(), threading.get_ident())
        with open(tmp, "w") as f:
            json.dump(_vk_rr_counter, f)
        os.replace(tmp, _RR_COUNTER_FILE)
    except Exception as e:
        print(f"[HM-RR] WARN could not save: {e}", file=sys.stderr, flush=True)

# Restore on import
_load_rr_counter()

def _next_hm_nv_key() -> int:
    """Sequential round-robin: k1→k2→k3→k4→k5→k1...
    Persistent counter — restart/power loss does NOT reset position.
    Every increment is immediately atomic persisted to rr_counter.json.

    Returns: 0-based key index (0..NV_NUM_KEYS-1)
    """
    with _vk_rr_lock:
        counter = _vk_rr_counter.get("hm_nv", 0)
        key_idx = counter % NV_NUM_KEYS
        _vk_rr_counter["hm_nv"] = counter + 1
        _save_rr_counter()  # Immediate persist — survive power loss
        return key_idx

# Signal handlers for clean shutdown
import atexit
import signal as _signal

def _flush_and_exit(signum, _frame):
    _save_rr_counter()
    raise SystemExit(128 + signum)

atexit.register(_save_rr_counter)
_signal.signal(_signal.SIGTERM, _flush_and_exit)
_signal.signal(_signal.SIGINT, _flush_and_exit)

# ─── Context window ──────────────────────────────────────────────────────
# NV models have various context windows; advertise safe capacity
MODEL_INPUT_TOKEN_SAFETY = {
    "kimi_hm": 131072,
    "glm5.1_hm": 170000,
    "minimax_hm": 131072,
    "deepseek_hm": 131072,
}
DEFAULT_CONTEXT_FALLBACK = 131072

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()
