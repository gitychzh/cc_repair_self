#!/usr/bin/env python3
"""Configuration constants and environment variables.

All configurable parameters are read from env vars with defaults.
Immutable constraints (variant model IDs, rpm=1, frontend model names,
container names, port assignments) are documented in CLAUDE.md.

R21: Added NUM_VARIANTS, VARIANT_IDS, v×k 2D round-robin support.
R23: Added AGENT_SUFFIXES, agent type detection, suffix-based model IDs.
R29: Added PROXY_ROLE, removed LiteLLM fallback.
Proxy precisely specifies variant+key combo → LiteLLM just forwards.
"""
import os
import sys
import json
import threading

# ─── Network ──────────────────────────────────────────────────────────────
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-local")
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "40001"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "300"))  # Overall request timeout concept (for docs)
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "60"))  # R27: Per-key HTTPConnection timeout, separated from PROXY_TIMEOUT

# ─── Proxy Role (R29) ────────────────────────────────────────────────────
# Each proxy container serves a specific role:
#   "cc"          → only /v1/messages (Anthropic format, CC), upstream=glm5.1
#   "codex"       → only /v1/responses (Responses API, Codex), upstream=glm5.1
#   "passthrough" → only /v1/chat/completions (OpenAI format, _ol/_oc), upstream=glm5.1
# This determines which endpoints to serve and which backend model to default to.
PROXY_ROLE = os.environ.get("PROXY_ROLE", "cc")

# ─── Role-based defaults ──────────────────────────────────────────────────
# Default upstream model based on role:
#   cc/codex → glm5.1 (CC and Codex need Anthropic/Responses format conversion)
#   passthrough → glm5.1 (OpenAI agents get nearly-transparent passthrough)
ROLE_DEFAULT_UPSTREAM = {
    "cc": "glm5.1",
    "codex": "glm5.1",
    "passthrough": "glm5.1",
}

# ─── Truncation limits ───────────────────────────────────────────────────
MAX_TOOL_DESC = int(os.environ.get("MAX_TOOL_DESC", "2000"))
MAX_SCHEMA_DESC = int(os.environ.get("MAX_SCHEMA_DESC", "600"))

# ─── Token estimation ────────────────────────────────────────────────────
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("CHARS_PER_TOKEN_ESTIMATE", "3.0"))

# ─── Logging ──────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# ─── URL helper ──────���────────────────────────────────────────────────────
def _ensure_url_path(url: str, path: str) -> str:
    """If env var provides only host or host/v1, append the required full path."""
    stripped = url.rstrip("/")
    if stripped.endswith(path):
        return url
    if stripped.endswith("/v1"):
        return stripped + path.replace("/v1", "", 1)
    return stripped + path

# ─── Per-model upstream routing ──────────────────────────────────────────
# R29: Single backend model (glm5.1), routed through ms_uni41001.
# LiteLLM fallback removed — single LiteLLM container only.
# Each proxy container uses its PROXY_ROLE to determine which backend to use.
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_GLM51", "http://ms_uni41001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_GLM51", "http://ms_uni41001:4000/v1/models"), "/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = ROLE_DEFAULT_UPSTREAM.get(PROXY_ROLE, "glm5.1")

# ─── Agent type suffixes (R23, R29 update) ────────────────────────────────
# Suffix determines: 1) Response format (anthropic/openai/responses)  2) Backend model  3) Error format
# "_cc" → Anthropic format, backend=glm5.1 (CC only, proxy 40001)
# "_cx" → Responses API format, backend=glm5.1 (Codex only, proxy 40002)
# "_ol/_oc" → OpenAI format, backend=glm5.1 (OpenAI agents, proxy 40003)
# R38: _hm removed — Hermes is an external app, uses hm40006 (NV) + 40003 (MS fallback) directly
AGENT_SUFFIXES = {
    "_cc": {"name": "Claude Code", "format": "anthropic", "backend": "glm5.1"},
    "_ol": {"name": "OpenClaw",    "format": "openai",    "backend": "glm5.1"},
    "_oc": {"name": "OpenCode",    "format": "openai",    "backend": "glm5.1"},
    "_cx": {"name": "Codex",       "format": "responses", "backend": "glm5.1"},
}
DEFAULT_AGENT_SUFFIX = "_cc"  # backward compat: no suffix = CC (Anthropic format)

# Base model names (backend routing targets)
BASE_MODELS = ["glm5.1"]

def detect_agent_type(model_id):
    """Detect agent type from model ID suffix.

    Args:
        model_id: model name, e.g. "glm5.1_cc", "glm5.1_ol", "glm5.1", "claude-opus-4-8"

    Returns:
        (base_model, agent_suffix, response_format)
        base_model: backend model name ("glm5.1")
        agent_suffix: "_cc", "_ol", "_oc" or DEFAULT_AGENT_SUFFIX
        response_format: "anthropic", "openai" or "responses"

    Examples:
        "glm5.1_cc" → ("glm5.1", "_cc", "anthropic")
        "glm5.1_ol" → ("glm5.1", "_ol", "openai")
        "glm5.1"    → ("glm5.1", "_cc", "anthropic")  # backward compat
        "claude-opus-4-8" → ("glm5.1", "_cc", "anthropic")  # MODEL_MAP lookup
    """
    # Check for explicit suffix
    for suffix, info in AGENT_SUFFIXES.items():
        if model_id.endswith(suffix):
            base = model_id[:-len(suffix)]
            # Validate base is a known backend model
            mapped = MODEL_MAP.get(base, None)
            if mapped and mapped in MODEL_UPSTREAMS:
                return (mapped, suffix, info["format"])
            # Base with suffix might be directly a backend model name
            if base in MODEL_UPSTREAMS:
                return (base, suffix, info["format"])

    # No suffix → default to CC (Anthropic format)
    # Try MODEL_MAP lookup first (e.g. "claude-opus-4-8" → "glm5.1")
    mapped = MODEL_MAP.get(model_id, None)
    if mapped and mapped in MODEL_UPSTREAMS:
        return (mapped, DEFAULT_AGENT_SUFFIX, AGENT_SUFFIXES[DEFAULT_AGENT_SUFFIX]["format"])

    # Direct backend model name (e.g. "glm5.1")
    if model_id in MODEL_UPSTREAMS:
        return (model_id, DEFAULT_AGENT_SUFFIX, AGENT_SUFFIXES[DEFAULT_AGENT_SUFFIX]["format"])

    # Unknown model → default based on PROXY_ROLE
    return (DEFAULT_UPSTREAM_MODEL, DEFAULT_AGENT_SUFFIX, AGENT_SUFFIXES[DEFAULT_AGENT_SUFFIX]["format"])

def format_model_id(base_model, agent_suffix):
    """Construct frontend model ID from base model + agent suffix.
    e.g. ("glm5.1", "_cc") → "glm5.1_cc", ("glm5.1", "_ol") → "glm5.1_ol"
    """
    return f"{base_model}{agent_suffix}"

# ─── Model name → LiteLLM model_name mapping ────────────────────────────
# NEVER change the variant model IDs — each has independent 200/id/day quota.
# All agent suffixes now route to glm5.1 backend (dsv4p removed — ModelScope delisted deepseek-v4-pro).
MODEL_MAP = {
    # ─── glm5.1 backend (all formats) ───
    # Claude Code (_cc) — Anthropic format
    "glm5.1_cc": "glm5.1",
    # Codex (_cx) — Responses API format
    "glm5.1_cx": "glm5.1",
    # OpenClaw (_ol) — OpenAI format
    "glm5.1_ol": "glm5.1",
    # OpenCode (_oc) — OpenAI format
    "glm5.1_oc": "glm5.1",
    # R38: glm5.1_hm removed from codex-proxy — Hermes uses hm40006 (NV) or 40003 (MS) directly

    # ─── Backward compat: no suffix = CC (Anthropic format) ───
    "glm5.1": "glm5.1", "glm-5.1": "glm5.1", "zhipuai/glm-5.1": "glm5.1",

    # Claude Code names → glm5.1 (CC, implicitly _cc / Anthropic format)
    "claude-opus-4-8": "glm5.1",
    "claude-opus-4-7": "glm5.1",
    "claude-opus-4": "glm5.1",
    "claude-sonnet-4-6": "glm5.1",
    "claude-sonnet-4": "glm5.1",
    "claude-haiku-4-5": "glm5.1",
    "claude-sonnet-4-20250514": "glm5.1",
    "claude-sonnet-4-6-20250514": "glm5.1",
    "claude-opus-4-20250514": "glm5.1",
    "claude-opus-4-8-20250514": "glm5.1",
    "claude-haiku-4-5-20251001": "glm5.1",
    "claude-3-5-sonnet-20241022": "glm5.1",
    "claude-3-5-haiku-20241022": "glm5.1",
    "claude-3-opus-20240229": "glm5.1",

    # OpenAI-style alias names → glm5.1 (for passthrough proxy, OpenAI format)
    "gpt-4o": "glm5.1",
    "gpt-4o-mini": "glm5.1",
    "o3": "glm5.1",
    "o3-mini": "glm5.1",
    "o4-mini": "glm5.1",
    "gpt-4.1": "glm5.1",
    "gpt-4.1-mini": "glm5.1",
    "gpt-4.1-nano": "glm5.1",
    # Codex CLI alias → glm5.1 (Codex专用, Responses API format)
    "codex-mini-latest": "glm5.1",
}

# Thinking support per backend model
# glm5.1 supports reasoning_effort + thinking_budget (ModelScope GLM-5.1 feature)
THINKING_SUPPORT = {"glm5.1": True}
DEFAULT_MODEL = ROLE_DEFAULT_UPSTREAM.get(PROXY_ROLE, "glm5.1")

# ─── Context-window budget system (R31.4) ────────────────────────────────
# Two layers per model:
#   MODEL_MAX_INPUT_TOKENS  — the BACKEND HARD CEILING enforced by ModelScope.
#                              Used for overflow-error detection only (NOT
#                              advertised to clients — advertising it invites
#                              them to fill up to the ceiling and hit 400).
#                              GLM-5.1 nominally supports 1M context upstream,
#                              but the ModelScope-hosted endpoint caps it at
#                              202745 (verified via its 400 error:
#                              "Range of input length should be [1, 202745]").
#   MODEL_INPUT_TOKEN_SAFETY — the SAFE CAPACITY advertised to clients in
#                              /v1/models (both OpenAI `context_length` and
#                              Anthropic `context_window`). Deliberately below
#                              the hard ceiling to leave headroom for:
#                                - output + thinking tokens (output/thinking
#                                  share the model's total window)
#                                - long-context quality degradation (effective
#                                  context < nominal; CC should compact before
#                                  the ceiling, never at it)
#                              This is the single source of truth for what we
#                              tell clients; tune via env, keep SAFETY < MAX.
#                              Pair with CC-side settings:
#                                contextWindow     = SAFETY
#                                autoCompactWindow = SAFETY - ~15000
DEFAULT_CONTEXT_FALLBACK = 131072  # generic fallback when a model isn't listed
MODEL_MAX_INPUT_TOKENS = {
    "glm5.1": 202745,
}
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.1": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_GLM51", "170000")),
}

# ─── Thinking config ─────────────────────────────────────────────────────
OUTPUT_TOKEN_MARGIN = 8192  # Room for output after thinking_budget
THINKING_SIGNATURE_DEFAULT = "ErUB3WY0k2GCM2h+4O0S3Y3W3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f"

# ─── Variant×Key 2D round-robin (R21) ──────────────────
# 2D round-robin: request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
# → model name: "glm5.1v{V}k{K}"
# On 429: same variant, cycle to next key (k→k+1). All 7 keys 429 → variant fallback (R23)
NUM_KEYS = int(os.environ.get("NUM_KEYS", "7"))
NUM_VARIANTS_GLM51 = int(os.environ.get("NUM_VARIANTS_GLM51", "10"))
NUM_VARIANTS = {"glm5.1": NUM_VARIANTS_GLM51}

# Variant model IDs — proxy uses these to construct precise model names.
# Each variant has independent 200/id/day quota on ModelScope. NEVER remove variants.
GLM51_VARIANT_IDS = [
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
VARIANT_IDS = {"glm5.1": GLM51_VARIANT_IDS}

_vk_rr_counter = {}  # model → int counter (0..∞), e.g. {"glm5.1": 0}
_vk_rr_lock = threading.Lock()

# R30/R31.3: Persist counter to disk so container restarts do NOT reset to v1k1.
# Without this, every --force-recreate (e.g. monitor.sh) dumps all fresh
# traffic onto v1~v3, blowing their RPM quota and causing the 429 storm.
#
# R31.3 hardening (this is the PRIMARY CC proxy now — must survive power loss):
#   - Persist EVERY increment immediately (was: every 10th). Atomic os.replace
#     is ~microseconds, negligible vs a ModelScope call (~1-5s).
#   - Register SIGTERM/SIGINT handler explicitly. Python atexit does NOT fire
#     on SIGTERM (i.e. `docker stop`), so the atexit-only approach lost the
#     tail. Now both fire; the per-increment write makes this belt-and-suspenders.
#   - _load tolerates empty/corrupt/truncated files cleanly (no WARN spam on
#     first boot after a log reset).
_RR_COUNTER_FILE = os.path.join(LOG_DIR, "rr_counter.json")

def _load_rr_counter() -> None:
    """Restore counters from disk at startup. Best-effort; on any error (missing,
    empty, or corrupt file), start fresh at 0 without raising."""
    try:
        with open(_RR_COUNTER_FILE, "r") as f:
            raw = f.read().strip()
        if not raw:
            return  # empty file (e.g. after log reset) — fresh start, silent
        saved = json.loads(raw)
        if isinstance(saved, dict):
            for k, v in saved.items():
                if isinstance(k, str) and isinstance(v, int) and v >= 0:
                    _vk_rr_counter[k] = v
            print(f"[RR-COUNTER] restored from {_RR_COUNTER_FILE}: {_vk_rr_counter}", file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass  # first boot — fine
    except (json.JSONDecodeError, ValueError) as e:
        # Corrupt/truncated file — log once and start fresh, do NOT crash
        print(f"[RR-COUNTER] file corrupt/empty ({e}); starting fresh at 0", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[RR-COUNTER] WARN could not load {_RR_COUNTER_FILE}: {e}; starting fresh", file=sys.stderr, flush=True)

def _save_rr_counter() -> None:
    """Persist counters to disk atomically (per-thread tmp file + os.replace).
    Best-effort; failures only logged to stderr — must NEVER raise, since this
    runs in the request hot path AND in signal handlers (main thread) concurrently
    with request threads. The unique tmp suffix avoids two concurrent saves
    clobbering each other's tmp file."""
    try:
        tmp = "%s.tmp.%d.%d" % (_RR_COUNTER_FILE, os.getpid(), threading.get_ident())
        with open(tmp, "w") as f:
            json.dump(_vk_rr_counter, f)
        os.replace(tmp, _RR_COUNTER_FILE)
    except Exception as e:
        print(f"[RR-COUNTER] WARN could not save {_RR_COUNTER_FILE}: {e}", file=sys.stderr, flush=True)

# Restore immediately on module import (before any request is served)
_load_rr_counter()

def _next_variant_key_pair(model: str) -> tuple:
    """Get next (variant_idx, key_idx) for 2D round-robin.
    Returns 0-based indices. variant_idx in [0, NUM_VARIANTS-1], key_idx in [0, NUM_KEYS-1].
    Counter N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
    R30: counter is persisted to disk so restarts don't reset it.
    R31.3: persist EVERY increment (was every 10th) so even power loss loses 0 steps.
    """
    num_variants = NUM_VARIANTS.get(model, 10)
    with _vk_rr_lock:
        counter = _vk_rr_counter.get(model, 0)
        variant_idx = (counter // NUM_KEYS) % num_variants
        key_idx = counter % NUM_KEYS
        _vk_rr_counter[model] = counter + 1
        _save_rr_counter()  # R31.3: immediate persist — survive power loss / SIGKILL
        return (variant_idx, key_idx)

# R31.3: flush on exit. atexit covers normal interpreter exit; explicit signal
# handlers cover SIGTERM/SIGINT (which is how `docker stop` and Ctrl-C kill the
# process — atexit does NOT fire for these). The handler saves then re-raises
# via SystemExit so the interpreter runs the rest of teardown cleanly.
import atexit
import signal as _signal

def _flush_and_exit(signum, _frame):
    _save_rr_counter()
    raise SystemExit(128 + signum)

atexit.register(_save_rr_counter)
_signal.signal(_signal.SIGTERM, _flush_and_exit)
_signal.signal(_signal.SIGINT, _flush_and_exit)

def _is_routing_name(name: str) -> bool:
    """Check if a model name is an internal variant×key routing name.
    R21: Routing names use v+k format. These are proxy→LiteLLM routing, NOT meant for agents.
    Checks for glm5.1 routing names."""
    for base in MODEL_UPSTREAMS:
        num_variants = NUM_VARIANTS.get(base, 10)
        for vi in range(num_variants):
            for ki in range(NUM_KEYS):
                if name == f"{base}v{vi+1}k{ki+1}":
                    return True
    return False

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()
