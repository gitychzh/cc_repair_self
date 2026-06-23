#!/usr/bin/env python3
"""Configuration for Hermes NV proxy (hm40006) — R38.3.

R38.2: Three-tier fallback routing: glm5.1 → kimi → deepseek.
R38.3→R38.4: Naming convention — dual suffix: agent_source + api_source.
       _hm_nv = Hermes + NVIDIA API (40006 hm-proxy, routed via LiteLLM → mihomo → US proxy)
       _hm_ms = Hermes + ModelScope API (40003 passthrough, ModelScope direct)
       Other agents: _cc (CC+MS), _ol (OpenClaw+MS), _oc (OpenCode+MS), _cx (Codex+MS)
       deepseek-v4-pro restored (tested via direct/US proxy/SG proxy — all OK;
       previous failures were transient mihomo proxy connection issues, not model).
       sock.settimeout() added for read timeout (R36.2 lesson applied to hm-proxy).

Example: glm5.1_hm_nv (Hermes→NV), glm5.1_hm_ms (Hermes→MS) — explicit agent+API distinction.
Hermes uses _hm_nv by default (primary=NV), falls back to _hm_ms via 40003 (MS).

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
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "45"))  # R38.5: 60→45 (NV/kimi/deepseek p95<30s)

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

# ─── Three-tier fallback model chain (R38.2→R38.4) ─────────────────────────
# R38.4: Dual suffix convention: _hm_nv = Hermes + NV API, _hm_ms = Hermes + MS API
# Priority order: glm5.1 (primary) → kimi (fallback 1)
# R38.6: deepseek_hm_nv REMOVED from fallback chain.
# Data evidence: 5/5 LiteLLM containers tested nvdeepseek_k1~k5 → all 30s timeout.
# NV API deepseek-v4-pro is completely unreachable (30s+ timeout on every request).
# Keeping deepseek in chain wasted 225s per request (5 keys × 45s) with zero success.
# deepseek config entries preserved in NV_MODEL_IDS/LITELLM_MODEL_MAP for future re-enablement
# if NV API restores deepseek-v4-pro availability.
NV_MODEL_TIERS = ["glm5.1_hm_nv", "kimi_hm_nv"]

NV_MODEL_IDS = {
    "glm5.1_hm_nv": "z-ai/glm-5.1",
    "kimi_hm_nv": "moonshotai/kimi-k2.6",
    "deepseek_hm_nv": "deepseek-ai/deepseek-v4-pro",  # preserved but not in tiers
}

# LiteLLM model name pattern: nv{model_short}_k{N}
LITELLM_MODEL_MAP = {
    "glm5.1_hm_nv": "nvglm5.1",
    "kimi_hm_nv": "nvkimi",
    "deepseek_hm_nv": "nvdeepseek",  # preserved but not in tiers
}

DEFAULT_NV_MODEL = "glm5.1_hm_nv"  # R38.4: _hm_nv dual suffix, glm5.1 as primary

# ─── Tier timeout budget (R38.6) ────────────────────────────────────────────
# Maximum time to spend on ONE tier before giving up and moving to next tier.
# Prevents cumulative timeout stacking (5 keys × 45s = 225s worst case per tier).
# Data: NV glm5.1 p95 < 30s, kimi p95 < 25s, direct test shows 2-5s success.
# With sock.settimeout(45) now working correctly, 90s budget allows ~2 timeout retries
# per tier, which is reasonable. If 2 keys timeout, the tier is likely broken anyway.
TIER_TIMEOUT_BUDGET_S = float(os.environ.get("TIER_TIMEOUT_BUDGET_S", "90"))

# ─── Agent suffix ──────────────────────────────────────────────────────────
# R38.4: Dual suffix — _hm_nv (Hermes + NV), _hm_ms (Hermes + MS)
AGENT_SUFFIXES = {
    "_hm_nv": {"name": "HermesNV", "format": "openai"},
}
DEFAULT_AGENT_SUFFIX = "_hm_nv"

# ─── Model name mapping ──────────────────────────────────────────────────
# Frontend model names → internal NV model keys
# R38.4: Dual suffix convention: _hm_nv (Hermes+NV), _hm_ms (Hermes+MS)
# Backward compat: old _nv/_hm names → _hm_nv equivalents
MODEL_MAP = {
    # Primary tier — NV API (Hermes)
    "glm5.1_hm_nv": "glm5.1_hm_nv",
    "glm5.1_nv": "glm5.1_hm_nv",       # R38.3 _nv alias → R38.4 _hm_nv
    "glm5.1": "glm5.1_hm_nv",          # Unqualified → NV (backward compat)
    "glm-5.1": "glm5.1_hm_nv",
    "z-ai/glm-5.1": "glm5.1_hm_nv",
    # Backward compat: old _hm names → _hm_nv (Hermes config migration)
    "glm5.1_hm": "glm5.1_hm_nv",
    # Fallback tier 1 — NV API (Hermes)
    "kimi_hm_nv": "kimi_hm_nv",
    "kimi_nv": "kimi_hm_nv",            # R38.3 alias → R38.4
    "kimi": "kimi_hm_nv",
    "kimi-k2.6": "kimi_hm_nv",
    "moonshotai/kimi-k2.6": "kimi_hm_nv",
    # Backward compat
    "kimi_hm": "kimi_hm_nv",
    # Fallback tier 2 — NV API (Hermes)
    "deepseek_hm_nv": "deepseek_hm_nv",
    "deepseek_nv": "deepseek_hm_nv",     # R38.3 alias → R38.4
    "deepseek": "deepseek_hm_nv",
    "deepseek-v4-pro": "deepseek_hm_nv",
    "deepseek-ai/deepseek-v4-pro": "deepseek_hm_nv",
    # Backward compat
    "deepseek_hm": "deepseek_hm_nv",
}

def detect_nv_model(model_id: str) -> str:
    """Detect NV model tier from frontend model name.

    Returns: internal NV model key (glm5.1_hm_nv/kimi_hm_nv/deepseek_hm_nv)
    Falls back to DEFAULT_NV_MODEL (glm5.1_hm_nv).
    """
    mapped = MODEL_MAP.get(model_id, None)
    if mapped and mapped in NV_MODEL_IDS:
        return mapped
    return DEFAULT_NV_MODEL

def get_tier_index(mapped_model: str) -> int:
    """Get the tier index for a mapped model.

    Returns: 0-based index in NV_MODEL_TIERS.
    Falls back to 0 (primary tier = glm5.1_hm_nv).
    """
    try:
        return NV_MODEL_TIERS.index(mapped_model)
    except ValueError:
        return 0

def litellm_model_name(mapped_model: str, key_idx: int) -> str:
    """Build LiteLLM model name for key_idx (0-based).

    e.g. mapped_model="glm5.1_hm_nv", key_idx=0 → "nvglm5.1_k1"
    """
    prefix = LITELLM_MODEL_MAP.get(mapped_model, "nvglm5.1")
    return f"{prefix}_k{key_idx + 1}"

# ─── Token estimation ──────────────────────────────────────────────────────
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("CHARS_PER_TOKEN_ESTIMATE", "3.0"))

# ─── Outbound throttle ──────────────────────────────────────────────────────
# R38.5: throttle only applies to first key attempt (not cycling).
# Cycling keys have independent RPM buckets — throttle delay is pure waste.
# R38.6: Default 1.5s (reduced from 2.0→3.5→1.5; 5 NV keys each have independent
# RPM=1/key, throttle only needs to prevent burst, 1.5s is sufficient).
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

# ─── 429 Cooldown tracking (R38.5→R38.6: restored + optimized) ──────────────
# When a key gets 429, mark it as "cooling" for KEY_COOLDOWN_S seconds.
# During cooldown, skip that key in the RR rotation to avoid wasting requests.
# R38.6 optimizations (data-driven):
# - KEY_COOLDOWN_S base: 10→15s (NV 429 recovery is ~15s based on burst throttle data;
#   CLAUDE.md says "429 root cause is RPM burst throttle, auto-recovers ~15min" — but
#   per-key RPM=1/key recovery is faster, ~15s per key)
# - Exponential backoff cap: 30s (prevent over-cooling)
# - Global tier cooldown on all-429: 15s (prevent immediate re-hit)
KEY_COOLDOWN_S = float(os.environ.get("KEY_COOLDOWN_S", "15.0"))
_key_cooldown_map = {}  # {(tier_model, key_idx): cooldown_until_timestamp}
_key_cooldown_lock = threading.Lock()

# R38.5: Exponential backoff tracking for 429s per key
# key -> consecutive_429_count, used to scale cooldown duration
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
    """Mark a key as cooling after receiving 429.

    R38.5: Exponential backoff with capped duration.
    Base: KEY_COOLDOWN_S (10s), doubles per consecutive 429, capped at 30s.
    """
    with _key429_lock:
        _key429_count[(tier_model, key_idx)] = _key429_count.get((tier_model, key_idx), 0) + 1
        consecutive = _key429_count[(tier_model, key_idx)]
    # R38.5: Exponential backoff: base KEY_COOLDOWN_S, double per consecutive 429, cap at 30s
    import math
    effective_duration = min(KEY_COOLDOWN_S * (2 ** (consecutive - 1)), 30) if duration_s is None else duration_s
    with _key_cooldown_lock:
        _key_cooldown_map[(tier_model, key_idx)] = time.monotonic() + effective_duration

def reset_key429_count(tier_model, key_idx):
    """Reset consecutive 429 count when a key succeeds."""
    with _key429_lock:
        _key429_count.pop((tier_model, key_idx), None)

# ─── Per-tier persistent round-robin counter (R38.2→R38.4) ─────────────────
# R38.4: Counter keys use hm_nv_ prefix (dual suffix convention).
# Old "nv_glm5.1"/"nv_kimi"/"nv_deepseek" (R38.3) counters are migrated.
# Oldest "hm_nv_glm5.1" (R38.2) also migrated.
_RR_COUNTER_FILE = os.path.join(LOG_DIR, "rr_counter.json")
_vk_rr_counter = {}
_vk_rr_lock = threading.Lock()

# Tier-specific RR counter keys (R38.4: hm_nv_ prefix)
_TIER_RR_KEYS = {
    "glm5.1_hm_nv": "hm_nv_glm5.1",
    "kimi_hm_nv": "hm_nv_kimi",
    "deepseek_hm_nv": "hm_nv_deepseek",
}

# R38.4 backward compat: old counter key names → new counter key names
_OLD_RR_KEY_MAP = {
    # R38.3 keys → R38.4
    "nv_glm5.1": "hm_nv_glm5.1",
    "nv_kimi": "hm_nv_kimi",
    "nv_deepseek": "hm_nv_deepseek",
    # R38.2 oldest keys (already match R38.4 format)
    "hm_nv_glm5.1": "hm_nv_glm5.1",
    "hm_nv_kimi": "hm_nv_kimi",
    "hm_nv_deepseek": "hm_nv_deepseek",
    # Oldest single counter
    "hm_nv": "hm_nv_glm5.1",
}

def _load_rr_counter() -> None:
    """Restore counters from disk at startup.

    R38.4: Migrates old nv_/hm_nv_ counter keys to hm_nv_ keys on first load.
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
    """Per-tier sequential round-robin: each tier tracks its own key position.

    R38.4: _hm_nv dual suffix for tier keys.
    This ensures fallback continues from current position (not k1).

    Args:
        tier_model: one of "glm5.1_hm_nv" / "kimi_hm_nv" / "deepseek_hm_nv"

    Returns: 0-based key index (0..HM_NUM_KEYS-1)
    """
    rr_key = _TIER_RR_KEYS.get(tier_model, "hm_nv_glm5.1")
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
    "glm5.1_hm_nv": 170000,
    "kimi_hm_nv": 131072,
    "deepseek_hm_nv": 131072,
}
DEFAULT_CONTEXT_FALLBACK = 131072

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()
