#!/usr/bin/env python3
"""Configuration for Hermes NV proxy (hm40006) — R38.

R38: hm40006 now routes through LiteLLM containers (41101-41105) instead of
direct HTTPS CONNECT tunnel to NV API. Each LiteLLM container has its own
per-key mihomo proxy (7894-7899) configured at the container level via
HTTPS_PROXY env var, ensuring IP diversity.

Chain: Hermes → hm40006 → LiteLLM 41101-41105 → mihomo per-key proxy → NV API
hm40006 does: model name mapping + 5-key sequential RR + MSG-FIX + throttle
LiteLLM does: NV API call (with drop_params for unsupported params)
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

# ─── LiteLLM upstream URLs (R38) ───────────────────────────────────────────
# 5 LiteLLM containers, each on its own port with per-key mihomo proxy
# Key1 → 41101 (mihomo 7894), Key2 → 41102 (mihomo 7895), etc.
HM_LITELLM_URLS = []
for i in range(1, 6):
    url = os.environ.get(f"HM_LITELLM_URL{i}", "")
    if url:
        HM_LITELLM_URLS.append(url)
HM_NUM_KEYS = len(HM_LITELLM_URLS)  # Should be 5
HM_LITELLM_KEY = os.environ.get("HM_LITELLM_KEY", "sk-litellm-local")

if HM_NUM_KEYS < 5:
    print(f"[HM-CONFIG] WARN: only {HM_NUM_KEYS} LiteLLM URLs configured (expected 5)", file=sys.stderr, flush=True)

# ─── NV model IDs on NVIDIA API ──────────────────────────────────────────────
# These map frontend model names to NV model IDs for LiteLLM config naming
NV_MODEL_IDS = {
    "kimi_hm": "moonshotai/kimi-k2.6",
    "glm5.1_hm": "z-ai/glm-5.1",
    "minimax_hm": "minimaxai/minimax-m3",
    "deepseek_hm": "deepseek-ai/deepseek-v4-pro",
}

# LiteLLM model name pattern: nv{model_short}_k{N}
# e.g. nvkimi_k1, nvglm5.1_k1, nvminimax_k1, nvdeepseek_k1
LITELLM_MODEL_MAP = {
    "kimi_hm": "nvkimi",
    "glm5.1_hm": "nvglm5.1",
    "minimax_hm": "nvminimax",
    "deepseek_hm": "nvdeepseek",
}

DEFAULT_NV_MODEL = "moonshotai/kimi-k2.6"

# ─── Agent suffix for Hermes ──────────────────────────────────────────────
AGENT_SUFFIXES = {
    "_hm": {"name": "Hermes", "format": "openai"},
}
DEFAULT_AGENT_SUFFIX = "_hm"

# ─── Model name mapping ──────────────────────────────────────────────────
# Frontend model names → internal NV model keys
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
    """Detect NV model key from frontend model name.

    Returns: internal NV model key (kimi_hm/glm5.1_hm/minimax_hm/deepseek_hm)
    Falls back to kimi_hm.
    """
    mapped = MODEL_MAP.get(model_id, None)
    if mapped and mapped in NV_MODEL_IDS:
        return mapped
    return "kimi_hm"  # Default = kimi

def litellm_model_name(mapped_model: str, key_idx: int) -> str:
    """Build LiteLLM model name for key_idx (0-based).

    e.g. mapped_model="kimi_hm", key_idx=0 → "nvkimi_k1"
    """
    prefix = LITELLM_MODEL_MAP.get(mapped_model, "nvkimi")
    return f"{prefix}_k{key_idx + 1}"

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

    Returns: 0-based key index (0..HM_NUM_KEYS-1)
    """
    with _vk_rr_lock:
        counter = _vk_rr_counter.get("hm_nv", 0)
        key_idx = counter % HM_NUM_KEYS
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
