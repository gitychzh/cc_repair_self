#!/usr/bin/env python3
"""Configuration constants and environment variables.

All configurable parameters are read from env vars with defaults.
Immutable constraints (variant model IDs, rpm=1, frontend model names,
container names, port assignments) are documented in CLAUDE.md.

R21: Added NUM_VARIANTS, VARIANT_IDS, v×k 2D round-robin support.
R23: Added AGENT_SUFFIXES, agent type detection, suffix-based model IDs.
R29: Added PROXY_ROLE, agent suffix-based routing, removed LiteLLM fallback.
R35.5: Removed dsv4p/deepseek-v4-pro (ModelScope delisted). All backends route to glm5.1 only.
R33.2: Added NV (NVIDIA) upstream — cc-proxy directly calls NVIDIA API via US proxy.
  NV has no RPM limit, no variants — simpler than MS.
  MS-NV interleaving: 12 slots (7 MS keys + 5 NV keys) in round-robin.
"""
import os
import sys
import json
import threading
import time

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
# R35.5: Only glm5.1 backend remains (dsv4p delisted from ModelScope).
# All roles route through ms_uni41001 for glm5.1.
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_GLM51", "http://ms_uni41001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_GLM51", "http://ms_uni41001:4000/v1/models"), "/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = ROLE_DEFAULT_UPSTREAM.get(PROXY_ROLE, "glm5.1")

# ─── NVIDIA (NV) upstream configuration (R33.2 → R36: per-key proxy) ────────
# cc-proxy calls NVIDIA API directly via US proxy (HTTPS_PROXY env).
# NV has no RPM limit, no variants — simpler upstream than MS.
# Only enabled for glm5.1 backend (CC proxy, port 40005).
# R36: Each NV key uses its own dedicated mihomo proxy port (fault isolation).
#   NV_PROXY_URL_MAP: JSON map {nv_key_idx → proxy URL}
#   If NV_PROXY_URL_MAP is not set, all keys share NV_PROXY_URL (backward compat).
NV_BASEURL = os.environ.get("NV_BASEURL", "")
NV_NUM_KEYS = int(os.environ.get("NV_NUM_KEYS", "0"))
NV_KEYS = []
for i in range(1, NV_NUM_KEYS + 1):
    key = os.environ.get(f"NV_KEY{i}", "")
    if key:
        NV_KEYS.append(key)
NV_PROXY_URL = os.environ.get("NV_PROXY_URL", "")  # Default HTTPS proxy (fallback)
NV_TIMEOUT = int(os.environ.get("NV_TIMEOUT", "60"))  # R36: 60s timeout (stability first)
NV_ENABLED = bool(NV_BASEURL and NV_KEYS)  # Auto-detect: enabled if keys+URL present

# NV proxy URL per key — each NV key uses its own dedicated mihomo proxy port
# R36: NV_PROXY_URL_MAP env var is a JSON string mapping nv_key_idx (str) → proxy URL
# Example: '{"0":"http://host.docker.internal:7894","1":"http://host.docker.internal:7895",...}'
NV_PROXY_URL_MAP = {}  # nv_key_idx (str) → proxy URL string
_nv_proxy_url_map_raw = os.environ.get("NV_PROXY_URL_MAP", "")
if _nv_proxy_url_map_raw:
    try:
        NV_PROXY_URL_MAP = json.loads(_nv_proxy_url_map_raw)
        # Validate: all keys must be strings, all values must be strings
        for k, v in NV_PROXY_URL_MAP.items():
            if not isinstance(k, str) or not isinstance(v, str):
                print(f"[NV] WARN: NV_PROXY_URL_MAP invalid entry {k}={v}, skipping", file=sys.stderr, flush=True)
                NV_PROXY_URL_MAP = {}
                break
    except json.JSONDecodeError as e:
        print(f"[NV] WARN: NV_PROXY_URL_MAP parse error ({e}), falling back to NV_PROXY_URL", file=sys.stderr, flush=True)
        NV_PROXY_URL_MAP = {}
if not NV_PROXY_URL_MAP and NV_PROXY_URL:
    # Fallback: all keys use same NV_PROXY_URL (backward compat with R33-R35)
    for i in range(NV_NUM_KEYS):
        NV_PROXY_URL_MAP[str(i)] = NV_PROXY_URL
    if NV_NUM_KEYS > 0:
        print(f"[NV] Using single NV_PROXY_URL for all {NV_NUM_KEYS} keys (backward compat)", file=sys.stderr, flush=True)

# Absolute cycle limit: when counter >= NV_MAX_CYCLE → reset to 0
# Prevents JSON file from growing indefinitely. 1200000 ≈ 12 slots × 100000 cycles
NV_MAX_CYCLE = int(os.environ.get("NV_MAX_CYCLE", "1200000"))

# NV model IDs on NVIDIA API — R38.7: 2-tier fallback (glm5.1→kimi, deepseek REMOVED)
# When NV last-resort triggers (MS all-429), tries each tier in order.
# Each tier tries all 5 NV keys (per-tier RR, persistent counter).
# Tier all-429/empty-200 → next tier. All tiers fail → ABORT-NO-FALLBACK.
# R38.7: NV_TIER_TIMEOUT_BUDGET_S caps total NV time at 90s (prevents 450s catastrophic blocking).
NV_MODEL_IDS = {
    "glm5.1": "z-ai/glm-5.1",
}

# R38.6→R38.7: NV 2-tier fallback model list per base model.
# deepseek-ai/deepseek-v4-flash REMOVED from NV fallback chain (R38.7).
# Data evidence: 5/5 NV keys all 30s+ timeout on deepseek-v4-flash — zero success rate.
# Keeping it wasted 225s per request with zero value.
# deepseek config entry preserved in NV_MODEL_IDS for future re-enablement if NV API restores it.
# Can be overridden via NV_FALLBACK_TIERS env var (JSON list of [model_id, label] pairs).
_NV_FALLBACK_TIERS_DEFAULT = {
    "glm5.1": [
        ("z-ai/glm-5.1",          "glm5.1_nv"),    # Tier 1: original model
        ("moonshotai/kimi-k2.6",  "kimi_nv"),      # Tier 2: kimi fallback (tested OK)
    ],
}

# R38.7: NV tier total timeout budget — prevents catastrophic cumulative timeouts.
# Without this: 5 keys × 30s timeout × 3 tiers = 450s max blocking time.
# With 90s budget: at most 3 keys × 30s in tier1 + 2-3 keys in tier2 before budget exhausts.
# Budget is checked BEFORE each key attempt. Exhausted → ABORT immediately, no more tiers.
NV_TIER_TIMEOUT_BUDGET_S = int(os.environ.get("NV_TIER_TIMEOUT_BUDGET_S", "90"))
_nv_fallback_tiers_env = os.environ.get("NV_FALLBACK_TIERS", "")
if _nv_fallback_tiers_env:
    try:
        # Env var format: JSON list of [model_id, label] pairs (applied to default model only)
        tiers_list = json.loads(_nv_fallback_tiers_env)
        if isinstance(tiers_list, list) and all(isinstance(t, list) and len(t) == 2 for t in tiers_list):
            NV_FALLBACK_TIERS = {
                DEFAULT_UPSTREAM_MODEL: [(t[0], t[1]) for t in tiers_list]
            }
            print(f"[NV-TIER] Loaded from env: {NV_FALLBACK_TIERS}", file=sys.stderr, flush=True)
        else:
            print(f"[NV-TIER] WARN: Invalid NV_FALLBACK_TIERS env format, using defaults", file=sys.stderr, flush=True)
            NV_FALLBACK_TIERS = _NV_FALLBACK_TIERS_DEFAULT
    except json.JSONDecodeError as e:
        print(f"[NV-TIER] WARN: NV_FALLBACK_TIERS parse error ({e}), using defaults", file=sys.stderr, flush=True)
        NV_FALLBACK_TIERS = _NV_FALLBACK_TIERS_DEFAULT
else:
    NV_FALLBACK_TIERS = _NV_FALLBACK_TIERS_DEFAULT

# Per-tier NV RR counters for 3-tier fallback (R38.6)
# Each tier has its own persistent counter so it continues from the last position
# (not restarting from k1 on each tier switch).
_nv_tier_rr_counters = {}  # key: "base_model:tier_idx" → int counter
_nv_tier_rr_lock = threading.Lock()
_NV_TIER_RR_FILE = os.path.join(LOG_DIR, "nv_tier_rr_counter.json")

# ─── MS-NV interleaving (R33.2 → R36: strict alternating) ───────────────────
# R36: Strict alternating pattern: ms→nv→ms→nv→ms→nv→...
# Total slots = NUM_KEYS (MS) + NV_NUM_KEYS (NV) = 7+5 = 12
# Slot assignment (strict alternating):
#   Even slots (0,2,4,6,8,10) → MS: key_idx = (slot//2) % NUM_KEYS
#   Odd slots (1,3,5,7,9,11) → NV: nv_key_idx = (slot//2) % NV_NUM_KEYS
#   Variant for MS: (counter // total_slots) % NUM_VARIANTS
#
# Example (cycle 0, counter 0-11):
#   0: ms(k1), 1: nv(k1→7894), 2: ms(k2), 3: nv(k2→7895), 4: ms(k3), 5: nv(k3→7896),
#   6: ms(k4), 7: nv(k4→7897), 8: ms(k5), 9: nv(k5→7899), 10: ms(k6), 11: nv(k1→7894)
#
# R33.2 old pattern (7MS then 5NV, no alternating) is replaced by R36 strict alternating.
MS_NV_TOTAL_SLOTS = None  # Computed after NUM_KEYS is defined (see below)

# ─── Agent type suffixes (R23, R29 update, R35.5 dsv4p removed) ───────────
# Suffix determines: 1) Response format (anthropic/openai/responses)  2) Backend model  3) Error format
# R35.5: All suffixes now route to glm5.1 backend (dsv4p delisted from ModelScope).
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
# R35.5: All entries route to glm5.1 backend only (dsv4p delisted from ModelScope).
MODEL_MAP = {
    # ─── glm5.1 backend (all agent types) ───
    # Claude Code (_cc) — Anthropic format
    "glm5.1_cc": "glm5.1",
    # Codex (_cx) — Responses API format
    "glm5.1_cx": "glm5.1",
    # OpenClaw/OpenCode (_ol/_oc) — OpenAI format
    "glm5.1_ol": "glm5.1",
    "glm5.1_oc": "glm5.1",
    # R38: glm5.1_hm removed from cc-proxy — Hermes uses hm40006 (NV) or 40003 (MS) directly

    # ─── Backward compat: no suffix = CC (Anthropic format) ───
    "glm5.1": "glm5.1", "glm-5.2": "glm5.1", "zhipuai/glm-5.2": "glm5.1",

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

# ─── Variant×Key 2D round-robin (R21, R35.5: only glm5.1) ──────────────────
# 2D round-robin: request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
# R31.9: reverted from diagonal to row-first (diagonal experiment disproved variant-bottleneck hypothesis).
# → model name: "glm5.1v{V}k{K}"
# On 429: same variant, cycle to next key (k→k+1). All 7 keys 429 → variant fallback (R23)
NUM_KEYS = int(os.environ.get("NUM_KEYS", "7"))
NUM_VARIANTS_GLM51 = int(os.environ.get("NUM_VARIANTS_GLM51", "10"))
NUM_VARIANTS = {"glm5.1": NUM_VARIANTS_GLM51}

# ─── Compute MS-NV interleaving total slots (after NUM_KEYS + NV_NUM_KEYS defined) ───
MS_NV_TOTAL_SLOTS = NUM_KEYS + NV_NUM_KEYS if NV_ENABLED else NUM_KEYS

# Variant model IDs — proxy uses these to construct precise model names.
# Each variant has independent 200/id/day quota on ModelScope. NEVER remove variants.
GLM51_VARIANT_IDS = [
    "ZHIPUAI/GLM-5.1",      # v1
    "ZHIPUAI/GLm-5.1",      # v2
    "ZHIPUAI/GlM-5.1",      # v3
    "ZHIPUAI/Glm-5.1",      # v4
    "ZHIPUAI/gLM-5.1",      # v5
    "ZHIPUAI/gLm-5.1",      # v6
    "ZHIPUAI/glM-5.1",      # v7
    "ZHIPUAI/glm-5.1",      # v8
    "ZHIPUAi/GLM-5.1",      # v9
    "ZHIPUAi/GLm-5.1",      # v10
]
VARIANT_IDS = {"glm5.1": GLM51_VARIANT_IDS}

_vk_rr_counter = {}  # model → int counter (0..∞), e.g. {"glm5.1": 0}
_vk_rr_lock = threading.Lock()

# ─── R31.9: Outbound request rate limiter (burst throttle mitigation) ────
# Forces a minimum interval between consecutive outbound requests to LiteLLM
# (ModelScope). Hypothesis: ModelScope's per-account/per-model RPM token-bucket
# is bursting because requests arrive too densely; spacing them by MIN_OUTBOUND_INTERVAL_S
# smooths the burst and reduces 429 throttling.
# Applies to EVERY outbound request regardless of outcome (success or 429/500/etc).
# Set to 0 to disable.
MIN_OUTBOUND_INTERVAL_S = float(os.environ.get("MIN_OUTBOUND_INTERVAL_S", "2.0"))
_outbound_last_sent = 0.0  # monotonic timestamp of last send
_outbound_throttle_lock = threading.Lock()


def throttle_outbound():
    """Enforce MIN_OUTBOUND_INTERVAL_S between consecutive outbound requests.
    Call this immediately before every conn.request("POST", ...).
    R36.3: Lock only for timestamp read/write — sleep outside the lock so
    concurrent handlers don't queue behind each other's sleeps.
    No-op if MIN_OUTBOUND_INTERVAL_S <= 0.
    """
    if MIN_OUTBOUND_INTERVAL_S <= 0:
        return
    global _outbound_last_sent
    with _outbound_throttle_lock:
        now = time.monotonic()
        elapsed = now - _outbound_last_sent
        wait = MIN_OUTBOUND_INTERVAL_S - elapsed
        # Reserve a slot: advance the timestamp immediately so the next
        # caller calculates its wait relative to THIS slot (not the previous).
        _outbound_last_sent = now if wait <= 0 else now + wait
    # Sleep outside the lock — other threads can enter and reserve their
    # slots while this one is sleeping.
    if wait > 0:
        time.sleep(wait)


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
    """Get next (variant_idx, key_idx, upstream_type, nv_key_idx, nv_proxy_url) for 2D round-robin.

    R36.5: MS-first + NV last-resort fallback. When NV_ENABLED and model="glm5.1":
      - Primary path: pure MS round-robin (same as NV_ENABLED=False)
      - NV is NOT forced into alternating slots (R36 strict alternating removed —
        data proved NV is net-negative: 31.5% success rate, 27% timeout at 40s,
        MS quota only 1.3% utilized, NV's "free quota" adds no value)
      - NV is only tried as LAST-RESORT fallback when ALL 7 MS keys 429 (ABORT path)
      - This gives MS ~100% of slots (vs R36's 58%), eliminating 40min/day NV timeout waste

    Returns 5-tuple: (variant_idx, key_idx, "ms", 0, NV_PROXY_URL)
    NV fallback in execute_request() picks NV key via round-robin on its own counter.

    R30: counter persisted to disk so restarts don't reset it.
    R31.3: persist EVERY increment so even power loss loses 0 steps.
    R36: absolute cycle — when counter >= NV_MAX_CYCLE → reset to 0 and continue.
    """
    num_variants = NUM_VARIANTS.get(model, 10)
    with _vk_rr_lock:
        counter = _vk_rr_counter.get(model, 0)

        # R36: Absolute cycle — reset when counter reaches MAX_CYCLE
        if counter >= NV_MAX_CYCLE:
            counter = 0
            _vk_rr_counter[model] = 0
            print(f"[RR-COUNTER] {model} counter reset to 0 (reached NV_MAX_CYCLE={NV_MAX_CYCLE})", file=sys.stderr, flush=True)
            _save_rr_counter()

        # R36.5: MS-first — ALWAYS pure MS round-robin for primary path
        # NV is now LAST-RESORT only (tried in execute_request when MS all-429)
        # This eliminates 41.7% forced NV slots that had 68.5% failure rate.
        # Counter increments by 1 per request (not by 1 per slot in 12-slot cycle).
        variant_idx = (counter // NUM_KEYS) % num_variants
        key_idx = counter % NUM_KEYS
        _vk_rr_counter[model] = counter + 1
        _save_rr_counter()
        return (variant_idx, key_idx, "ms", 0, NV_PROXY_URL)

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

# ─── Per-tier NV RR counter persistence (R38.6) ────────────────────────────
def _load_nv_tier_rr_counter() -> None:
    """Restore per-tier NV RR counters from disk. Best-effort."""
    try:
        with open(_NV_TIER_RR_FILE, "r") as f:
            raw = f.read().strip()
        if not raw:
            return
        saved = json.loads(raw)
        if isinstance(saved, dict):
            for k, v in saved.items():
                if isinstance(k, str) and isinstance(v, int) and v >= 0:
                    _nv_tier_rr_counters[k] = v
            if _nv_tier_rr_counters:
                print(f"[NV-TIER-RR] restored from {_NV_TIER_RR_FILE}: {_nv_tier_rr_counters}", file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[NV-TIER-RR] WARN could not load {_NV_TIER_RR_FILE}: {e}; starting fresh", file=sys.stderr, flush=True)

def _save_nv_tier_rr_counter() -> None:
    """Persist per-tier NV RR counters to disk atomically."""
    try:
        tmp = "%s.tmp.%d.%d" % (_NV_TIER_RR_FILE, os.getpid(), threading.get_ident())
        with open(tmp, "w") as f:
            json.dump(_nv_tier_rr_counters, f)
        os.replace(tmp, _NV_TIER_RR_FILE)
    except Exception as e:
        print(f"[NV-TIER-RR] WARN could not save {_NV_TIER_RR_FILE}: {e}", file=sys.stderr, flush=True)

# Restore on module import
_load_nv_tier_rr_counter()

def _next_nv_tier_key(base_model, tier_idx):
    """Get next NV key index for a specific tier (per-tier persistent RR).

    Each tier has its own counter so it continues from the last position,
    not restarting from k1 when switching tiers.
    Returns: nv_key_idx (0-based)
    """
    counter_key = f"{base_model}:{tier_idx}"
    with _nv_tier_rr_lock:
        counter = _nv_tier_rr_counters.get(counter_key, 0)
        nv_key_idx = counter % NV_NUM_KEYS
        _nv_tier_rr_counters[counter_key] = counter + 1
        _save_nv_tier_rr_counter()
    return nv_key_idx

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()
