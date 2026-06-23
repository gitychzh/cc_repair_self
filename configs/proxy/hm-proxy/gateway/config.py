#!/usr/bin/env python3
"""Configuration for Hermes NV proxy (hm40006) — R38.3.

R38.2: Three-tier fallback routing: glm5.1 → kimi → deepseek.
R38.3: Model suffix _hm → _nv to distinguish NV API models from MS API models.
       deepseek-v4-pro restored (tested via direct/US proxy/SG proxy — all OK;
       previous failures were transient mihomo proxy connection issues, not model).
       sock.settimeout() added for read timeout (R36.2 lesson applied to hm-proxy).

Naming convention:
  _nv suffix = NV API model (40006 hm-proxy, routed via LiteLLM → mihomo → US proxy)
  _hm suffix = MS API model (40003 passthrough, ModelScope direct)

  Example: glm5.1_nv (NV), glm5.1_hm (MS) — explicit distinction.
  Hermes uses _nv by default (primary=NV), falls back to _hm via 40003 (MS).

Each tier uses 5 keys (k1→k5) with per-tier persistent RR counter.
Fallback triggers: all 5 keys 429 or empty 200 (choices=null/content=null).
Fallback continues from current key position (not from k1).

Chain: Hermes → hm40006 → LiteLLM 41101-41105 → mihomo per-key proxy → NV API
hm40006 does: model tier selection + per-tier 5-key RR + MSG-FIX + throttle + 3-tier fallback
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

# ─── Three-tier fallback model chain (R38.2→R38.3) ─────────────────────────
# R38.3: _hm → _nv suffix. NV API models use _nv, MS API models use _hm.
# Priority order: glm5.1 (primary) → kimi (fallback 1) → deepseek (fallback 2)
# Default model = glm5.1_nv (highest quality, NV API)
NV_MODEL_TIERS = ["glm5.1_nv", "kimi_nv", "deepseek_nv"]

NV_MODEL_IDS = {
    "glm5.1_nv": "z-ai/glm-5.1",
    "kimi_nv": "moonshotai/kimi-k2.6",
    "deepseek_nv": "deepseek-ai/deepseek-v4-pro",
}

# LiteLLM model name pattern: nv{model_short}_k{N}
LITELLM_MODEL_MAP = {
    "glm5.1_nv": "nvglm5.1",
    "kimi_nv": "nvkimi",
    "deepseek_nv": "nvdeepseek",
}

DEFAULT_NV_MODEL = "glm5.1_nv"  # R38.3: _nv suffix, glm5.1 as primary

# ─── Agent suffix ──────────────────────────────────────────────────────────
# Hermes agent uses _nv suffix to identify NV API models
AGENT_SUFFIXES = {
    "_nv": {"name": "HermesNV", "format": "openai"},
}
DEFAULT_AGENT_SUFFIX = "_nv"

# ─── Model name mapping ──────────────────────────────────────────────────
# Frontend model names → internal NV model keys
# R38.3: _nv suffix for NV models; _hm aliases kept for backward compat
# (Hermes previously used _hm to route to 40006; now _hm means MS/40003)
MODEL_MAP = {
    # Primary tier — NV API
    "glm5.1_nv": "glm5.1_nv",
    "glm5.1": "glm5.1_nv",        # Unqualified → NV (backward compat)
    "glm-5.1": "glm5.1_nv",
    "z-ai/glm-5.1": "glm5.1_nv",
    # Backward compat: old _hm names → NV (Hermes config migration)
    "glm5.1_hm": "glm5.1_nv",
    # Fallback tier 1 — NV API
    "kimi_nv": "kimi_nv",
    "kimi": "kimi_nv",
    "kimi-k2.6": "kimi_nv",
    "moonshotai/kimi-k2.6": "kimi_nv",
    # Backward compat
    "kimi_hm": "kimi_nv",
    # Fallback tier 2 — NV API
    "deepseek_nv": "deepseek_nv",
    "deepseek": "deepseek_nv",
    "deepseek-v4-pro": "deepseek_nv",
    "deepseek-ai/deepseek-v4-pro": "deepseek_nv",
    # Backward compat
    "deepseek_hm": "deepseek_nv",
}

def detect_nv_model(model_id: str) -> str:
    """Detect NV model tier from frontend model name.

    Returns: internal NV model key (glm5.1_nv/kimi_nv/deepseek_nv)
    Falls back to DEFAULT_NV_MODEL (glm5.1_nv).
    """
    mapped = MODEL_MAP.get(model_id, None)
    if mapped and mapped in NV_MODEL_IDS:
        return mapped
    return DEFAULT_NV_MODEL

def get_tier_index(mapped_model: str) -> int:
    """Get the tier index for a mapped model.

    Returns: 0-based index in NV_MODEL_TIERS.
    Falls back to 0 (primary tier = glm5.1_nv).
    """
    try:
        return NV_MODEL_TIERS.index(mapped_model)
    except ValueError:
        return 0

def litellm_model_name(mapped_model: str, key_idx: int) -> str:
    """Build LiteLLM model name for key_idx (0-based).

    e.g. mapped_model="glm5.1_nv", key_idx=0 → "nvglm5.1_k1"
    """
    prefix = LITELLM_MODEL_MAP.get(mapped_model, "nvglm5.1")
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

# ─── Per-tier persistent round-robin counter (R38.2→R38.3) ─────────────────
# R38.3: Counter keys renamed from _hm → _nv suffix.
# Old "hm_nv_glm5.1"/"hm_nv_kimi"/"hm_nv_deepseek" counters are migrated.
_RR_COUNTER_FILE = os.path.join(LOG_DIR, "rr_counter.json")
_vk_rr_counter = {}
_vk_rr_lock = threading.Lock()

# Tier-specific RR counter keys (R38.3: _nv suffix)
_TIER_RR_KEYS = {
    "glm5.1_nv": "nv_glm5.1",
    "kimi_nv": "nv_kimi",
    "deepseek_nv": "nv_deepseek",
}

# R38.3 backward compat: old counter key names → new counter key names
_OLD_RR_KEY_MAP = {
    "hm_nv_glm5.1": "nv_glm5.1",
    "hm_nv_kimi": "nv_kimi",
    "hm_nv_deepseek": "nv_deepseek",
    "hm_nv": "nv_glm5.1",  # oldest single counter → glm5.1 tier
}

def _load_rr_counter() -> None:
    """Restore counters from disk at startup.

    R38.3: Migrates old _hm counter keys to _nv keys on first load.
    """
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
                    # Check if this is an old key that needs migration
                    new_key = _OLD_RR_KEY_MAP.get(k, k)
                    if new_key != k:
                        _vk_rr_counter[new_key] = v
                        migrated = True
                    else:
                        _vk_rr_counter[k] = v
            if migrated:
                _log_migration(f"Migrated old _hm RR keys → _nv keys: {saved} → {_vk_rr_counter}")
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
    """Per-tier sequential round-robin: each tier tracks its own key position.

    R38.3: _nv suffix for tier keys.
    This ensures fallback continues from current position (not k1).

    Args:
        tier_model: one of "glm5.1_nv" / "kimi_nv" / "deepseek_nv"

    Returns: 0-based key index (0..HM_NUM_KEYS-1)
    """
    rr_key = _TIER_RR_KEYS.get(tier_model, "nv_glm5.1")
    with _vk_rr_lock:
        counter = _vk_rr_counter.get(rr_key, 0)
        key_idx = counter % HM_NUM_KEYS
        _vk_rr_counter[rr_key] = counter + 1
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
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.1_nv": 170000,
    "kimi_nv": 131072,
    "deepseek_nv": 131072,
}
DEFAULT_CONTEXT_FALLBACK = 131072

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()
