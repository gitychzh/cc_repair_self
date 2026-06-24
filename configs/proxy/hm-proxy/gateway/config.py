#!/usr/bin/env python3
"""Configuration for Hermes NV proxy — R38.14.

R38.14: Tier reorder — glm5.1 primary (best agent quality for Hermes) → deepseek fallback → kimi last-resort.
R38.12: ALL models use NVCF pexec direct path (SOCKS5 → ACTIVE functions).
        LiteLLM 41101-41105 removed from active routing (kept as manual fallback only).
        strip_params per-model declaration: glm5.1 strips thinking_budget (NVCF rejects it),
        deepseek/kimi pass all params through (NVCF accepts them).

Chain (ALL models): Hermes → hm40006 → NVCF pexec (per-model ACTIVE function) → per-key SOCKS5 proxy → mihomo → NV API

Each tier uses 5 keys (k1→k5) with per-tier persistent RR counter.
Fallback triggers: all 5 keys 429 or empty 200 (choices=null/content=null).
Fallback continues from current key position (not from k1).

Tier order: glm5.1_hm_nv (primary, best agent quality) → deepseek_hm_nv → kimi_hm_nv (last-resort)
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
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "45"))  # R38.5: 60→45 (NV/kimi/deepseek p95<30s)

# ─── Proxy Role ────────────────────────────────────────────────────────────
# "passthrough" — serves /v1/chat/completions (OpenAI format)
PROXY_ROLE = os.environ.get("PROXY_ROLE", "passthrough")

# ─── Logging ──────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# ─── NVCF pexec configuration (R38.12: ALL models) ──────────────────────────
# R38.12: All 3 models now use NVCF pexec direct path (bypasses integrate API).
# Each model targets a specific ACTIVE NVCF function ID, env var overrideable.
# strip_params: per-model declaration of params that NVCF pexec rejects.
#   - deepseek/kimi: NVCF pexec accepts all params (thinking_budget, reasoning_effort)
#   - glm5.1: NVCF pexec REJECTS thinking_budget (returns 400) → must strip it
#     reasoning_effort is OK (tested 200 OK)
NVCF_BASE_URL = os.environ.get("NVCF_BASE_URL", "api.nvcf.nvidia.com")
NVCF_PEXEC_MODELS = {
    "deepseek_hm_nv": {
        "function_id": os.environ.get("NVCF_DEEPSEEK_FUNCTION_ID",
                                      "4e533b45-dc54-4e3a-a69a-6ff24e048cb5"),  # orion-deepseek-v4-pro (ACTIVE)
        "strip_params": [],  # NVCF pexec accepts all params for deepseek ✅
    },
    "kimi_hm_nv": {
        "function_id": os.environ.get("NVCF_KIMI_FUNCTION_ID",
                                      "f966661c-790d-4f71-b973-c525fb8eafd4"),  # nvquery-kimi-k2.6 (ACTIVE)
        "strip_params": [],  # NVCF pexec accepts all params for kimi ✅
    },
    "glm5.1_hm_nv": {
        "function_id": os.environ.get("NVCF_GLM51_FUNCTION_ID",
                                      "822231fa-d4f3-44dd-8057-be52cc344c1d"),  # ai-glm5_1 (ACTIVE)
        "strip_params": ["thinking_budget"],  # NVCF pexec REJECTS thinking_budget → 400 ❌
    },
}

# ─── NV API keys for NVCF pexec (all models use same 5 keys) ──────────────
HM_NV_KEYS = []
for i in range(1, 6):
    key = os.environ.get(f"HM_NV_KEY{i}", "")
    if key:
        HM_NV_KEYS.append(key)
HM_NUM_KEYS = len(HM_NV_KEYS)

# ─── Per-key mihomo SOCKS5 proxy URLs ──────────────────────────────────────
# K1→7894, K2→7895, K3→7896, K4→7897, K5→7899
HM_NV_PROXY_URLS = []
for i in range(1, 6):
    url = os.environ.get(f"HM_NV_PROXY_URL{i}", "")
    if url:
        HM_NV_PROXY_URLS.append(url)

if HM_NUM_KEYS < 5:
    print(f"[HM-CONFIG] WARN: only {HM_NUM_KEYS} NV keys configured (expected 5)", file=sys.stderr, flush=True)

# ─── Three-tier fallback model chain (R38.14) ──────────────────────────────
# R38.14: glm5.1 primary (best agent quality for Hermes) → deepseek fallback → kimi last-resort
NV_MODEL_TIERS = ["glm5.1_hm_nv", "deepseek_hm_nv", "kimi_hm_nv"]

NV_MODEL_IDS = {
    "glm5.1_hm_nv": "z-ai/glm-5.1",
    "kimi_hm_nv": "moonshotai/kimi-k2.6",
    "deepseek_hm_nv": "deepseek-ai/deepseek-v4-pro",
}

DEFAULT_NV_MODEL = "glm5.1_hm_nv"  # R38.14: glm5.1 primary (best agent quality for Hermes)

# ─── Tier timeout budget ──────────────────────────────────────────────────
TIER_TIMEOUT_BUDGET_S = float(os.environ.get("TIER_TIMEOUT_BUDGET_S", "60"))

# ─── Agent suffix ──────────────────────────────────────────────────────────
AGENT_SUFFIXES = {
    "_hm_nv": {"name": "HermesNV", "format": "openai"},
}
DEFAULT_AGENT_SUFFIX = "_hm_nv"

# ─── Model name mapping ──────────────────────────────────────────────────
MODEL_MAP = {
    # Primary tier — glm5.1 (NVCF pexec ai-glm5_1 ACTIVE, best agent quality)
    "glm5.1_hm_nv": "glm5.1_hm_nv",
    "glm5.1_nv": "glm5.1_hm_nv",
    "glm-5.1": "glm5.1_hm_nv",
    "z-ai/glm-5.1": "glm5.1_hm_nv",
    "glm5.1_hm": "glm5.1_hm_nv",
    # Fallback tier 1 — deepseek (NVCF pexec orion ACTIVE)
    "deepseek_hm_nv": "deepseek_hm_nv",
    "deepseek_nv": "deepseek_hm_nv",
    "deepseek": "deepseek_hm_nv",
    "deepseek-v4-pro": "deepseek_hm_nv",
    "deepseek-ai/deepseek-v4-pro": "deepseek_hm_nv",
    "deepseek_hm": "deepseek_hm_nv",
    # Last-resort tier — kimi (NVCF pexec nvquery-kimi ACTIVE)
    "kimi_hm_nv": "kimi_hm_nv",
    "kimi_nv": "kimi_hm_nv",
    "kimi": "kimi_hm_nv",
    "kimi-k2.6": "kimi_hm_nv",
    "moonshotai/kimi-k2.6": "kimi_hm_nv",
    "kimi_hm": "kimi_hm_nv",
}

def detect_nv_model(model_id: str) -> str:
    """Detect NV model tier from frontend model name.

    Returns: internal NV model key (deepseek_hm_nv/kimi_hm_nv/glm5.1_hm_nv)
    Falls back to DEFAULT_NV_MODEL (deepseek_hm_nv).
    """
    mapped = MODEL_MAP.get(model_id, None)
    if mapped and mapped in NV_MODEL_IDS:
        return mapped
    return DEFAULT_NV_MODEL

def get_tier_index(mapped_model: str) -> int:
    """Get the tier index for a mapped model."""
    try:
        return NV_MODEL_TIERS.index(mapped_model)
    except ValueError:
        return 0

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

# ─── 429 Cooldown tracking ────────────────────────────────────────────────
KEY_COOLDOWN_S = float(os.environ.get("KEY_COOLDOWN_S", "15.0"))
_key_cooldown_map = {}
_key_cooldown_lock = threading.Lock()

_key429_count = {}
_key429_lock = threading.Lock()

def is_key_cooling(tier_model, key_idx):
    """Check if a key is in cooldown (recently got 429)."""
    with _key_cooldown_lock:
        cooldown_until = _key_cooldown_map.get((tier_model, key_idx), 0)
        if cooldown_until > time.monotonic():
            return True
        return False

def mark_key_cooling(tier_model, key_idx, duration_s=None):
    """Mark a key as cooling after receiving 429. Exponential backoff, capped at 30s."""
    with _key429_lock:
        _key429_count[(tier_model, key_idx)] = _key429_count.get((tier_model, key_idx), 0) + 1
        consecutive = _key429_count[(tier_model, key_idx)]
    import math
    effective_duration = min(KEY_COOLDOWN_S * (2 ** (consecutive - 1)), 30) if duration_s is None else duration_s
    with _key_cooldown_lock:
        _key_cooldown_map[(tier_model, key_idx)] = time.monotonic() + effective_duration

def reset_key429_count(tier_model, key_idx):
    """Reset consecutive 429 count when a key succeeds."""
    with _key429_lock:
        _key429_count.pop((tier_model, key_idx), None)

# ─── Per-tier persistent round-robin counter ───────────────────────────────
_RR_COUNTER_FILE = os.path.join(LOG_DIR, "rr_counter.json")
_vk_rr_counter = {}
_vk_rr_lock = threading.Lock()

_TIER_RR_KEYS = {
    "glm5.1_hm_nv": "hm_nv_glm5.1",
    "kimi_hm_nv": "hm_nv_kimi",
    "deepseek_hm_nv": "hm_nv_deepseek",
}

_OLD_RR_KEY_MAP = {
    "nv_glm5.1": "hm_nv_glm5.1",
    "nv_kimi": "hm_nv_kimi",
    "nv_deepseek": "hm_nv_deepseek",
    "hm_nv_glm5.1": "hm_nv_glm5.1",
    "hm_nv_kimi": "hm_nv_kimi",
    "hm_nv_deepseek": "hm_nv_deepseek",
    "hm_nv": "hm_nv_glm5.1",
}

def _load_rr_counter() -> None:
    """Restore counters from disk at startup, migrating old key names."""
    try:
        with open(_RR_COUNTER_FILE, "r") as f:
            raw = f.read().strip()
        if not raw:
            return
        saved = json.loads(raw)
        if isinstance(saved, dict):
            migrated = False
            for k, v in saved.items():
                if isinstance(k, str) and isinstance(v, int) and v >= 0:
                    new_key = _OLD_RR_KEY_MAP.get(k, k)
                    if new_key != k:
                        _vk_rr_counter[new_key] = v
                        migrated = True
                    else:
                        _vk_rr_counter[k] = v
            if migrated:
                _log_migration(f"Migrated old RR keys → hm_nv_ keys: {saved} → {_vk_rr_counter}")
                _save_rr_counter()
            print(f"[HM-RR] restored from {_RR_COUNTER_FILE}: {_vk_rr_counter}", file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[HM-RR] file corrupt ({e}); starting fresh", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[HM-RR] WARN could not load: {e}", file=sys.stderr, flush=True)

def _log_migration(msg: str) -> None:
    """Log counter migration events."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date = time.strftime("%Y-%m-%d")
        with open(os.path.join(LOG_DIR, f"hm_proxy.{date}.log"), "a") as f:
            ts = time.strftime("%H:%M:%S")
            f.write(f"[{ts}] [MIGRATE] {msg}\n")
    except Exception:
        pass

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

def _next_hm_nv_key(tier_model: str) -> int:
    """Per-tier sequential round-robin: each tier tracks its own key position."""
    rr_key = _TIER_RR_KEYS.get(tier_model, "hm_nv_glm5.1")
    with _vk_rr_lock:
        counter = _vk_rr_counter.get(rr_key, 0)
        key_idx = counter % HM_NUM_KEYS
        _vk_rr_counter[rr_key] = counter + 1
        _save_rr_counter()
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
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.1_hm_nv": 170000,
    "kimi_hm_nv": 131072,
    "deepseek_hm_nv": 131072,
}
DEFAULT_CONTEXT_FALLBACK = 131072

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()
