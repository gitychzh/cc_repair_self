#!/usr/bin/env python3
"""Configuration constants and environment variables.

All configurable parameters are read from env vars with defaults.
Immutable constraints (variant model IDs, rpm=1, frontend model names,
container names, port assignments) are documented in CLAUDE.md.

R21: Added NUM_VARIANTS, VARIANT_IDS, v×k 2D round-robin support.
R23: Added AGENT_SUFFIXES, agent type detection, suffix-based model IDs.
Proxy precisely specifies variant+key combo → LiteLLM just forwards.
"""
import os
import threading

# ─── Network ──────────────────────────────────────────────────────────────
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-local")
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "40001"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "300"))

# ─── Truncation limits ───────────────────────────────────────────────────
MAX_TOOL_DESC = int(os.environ.get("MAX_TOOL_DESC", "2000"))
MAX_SCHEMA_DESC = int(os.environ.get("MAX_SCHEMA_DESC", "600"))

# ─── Token estimation ────────────────────────────────────────────────────
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("CHARS_PER_TOKEN_ESTIMATE", "2.0"))

# ─── Logging ──────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# ─── URL helper ───────────────────────────────────────────────────────────
def _ensure_url_path(url: str, path: str) -> str:
    """If env var provides only host or host/v1, append the required full path."""
    stripped = url.rstrip("/")
    if stripped.endswith(path):
        return url
    if stripped.endswith("/v1"):
        return stripped + path.replace("/v1", "", 1)
    return stripped + path

# ─── Per-model upstream routing ──────────────────────────────────────────
# R21: Both models route to ms_uni41001 (unified container with 140 dep)
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_GLM51", "http://ms_uni41001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_GLM51", "http://ms_uni41001:4000/v1/models"), "/v1/models"),
    },
    "dsv4p": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_DSV4P", "http://ms_uni41001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_DSV4P", "http://ms_uni41001:4000/v1/models"), "/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = "glm5.1"

# ─── Agent type suffixes (R23) ────────────────────────────────────────────
# Suffix determines: 1) Response format (anthropic vs openai)  2) Error format
# "_cc" → Anthropic format (Claude Code)
# "_ol" → OpenAI format (OpenClaw)
# "_oc" → OpenAI format (OpenCode)
# "_hm" → OpenAI format (Hermes)
AGENT_SUFFIXES = {
    "_cc": {"name": "Claude Code", "format": "anthropic"},
    "_ol": {"name": "OpenClaw",    "format": "openai"},
    "_oc": {"name": "OpenCode",    "format": "openai"},
    "_hm": {"name": "Hermes",      "format": "openai"},
}
DEFAULT_AGENT_SUFFIX = "_cc"  # backward compat: no suffix = CC (Anthropic format)

# Base model names (backend routing targets)
BASE_MODELS = ["glm5.1", "dsv4p"]

def detect_agent_type(model_id):
    """Detect agent type from model ID suffix.

    Args:
        model_id: model name, e.g. "glm5.1_cc", "glm5.1_ol", "glm5.1", "claude-opus-4-8"

    Returns:
        (base_model, agent_suffix, response_format)
        base_model: backend model name ("glm5.1" or "dsv4p")
        agent_suffix: "_cc", "_ol", "_oc", "_hm" or DEFAULT_AGENT_SUFFIX
        response_format: "anthropic" or "openai"

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

    # Unknown model → default to glm5.1 with CC format
    return (DEFAULT_UPSTREAM_MODEL, DEFAULT_AGENT_SUFFIX, AGENT_SUFFIXES[DEFAULT_AGENT_SUFFIX]["format"])

def format_model_id(base_model, agent_suffix):
    """Construct frontend model ID from base model + agent suffix.
    e.g. ("glm5.1", "_cc") → "glm5.1_cc"
    """
    return f"{base_model}{agent_suffix}"

# ─── Model name → LiteLLM model_name mapping ────────────────────────────
# NEVER change the variant model IDs — each has independent 200/id/day quota.
# R23: Added suffix-based entries for multi-agent routing.
# Suffix determines response format; MODEL_MAP determines backend routing.
MODEL_MAP = {
    # R23: Suffix-based model IDs — suffix determines format, base determines backend
    # Claude Code (_cc) — Anthropic format
    "glm5.1_cc": "glm5.1", "dsv4p_cc": "dsv4p",
    # OpenClaw (_ol) — OpenAI format
    "glm5.1_ol": "glm5.1", "dsv4p_ol": "dsv4p",
    # OpenCode (_oc) — OpenAI format
    "glm5.1_oc": "glm5.1", "dsv4p_oc": "dsv4p",
    # Hermes (_hm) — OpenAI format
    "glm5.1_hm": "glm5.1", "dsv4p_hm": "dsv4p",

    # Backward compat: no suffix = CC (Anthropic format)
    "glm5.1": "glm5.1", "glm-5.1": "glm5.1", "zhipuai/glm-5.1": "glm5.1",
    "dsv4p": "dsv4p", "deepseek-v4-pro": "dsv4p", "deepseek-ai/deepseek-v4-pro": "dsv4p",

    # Claude Code names → glm5.1 (implicitly _cc / Anthropic format)
    # ALL Claude opus/sonnet names → glm5.1 for maximum quota capacity
    "claude-opus-4-8": "glm5.1",
    "claude-opus-4-7": "glm5.1",
    "claude-opus-4": "glm5.1",
    "claude-sonnet-4-6": "glm5.1",
    "claude-sonnet-4": "glm5.1",
    "claude-haiku-4-5": "dsv4p",  # haiku tier → dsv4p (lighter backend, fast responses)
    "claude-sonnet-4-20250514": "glm5.1",
    "claude-sonnet-4-6-20250514": "glm5.1",
    "claude-opus-4-20250514": "glm5.1",
    "claude-opus-4-8-20250514": "glm5.1",
    "claude-haiku-4-5-20251001": "dsv4p",  # haiku tier → dsv4p
    "claude-3-5-sonnet-20241022": "glm5.1",
    "claude-3-5-haiku-20241022": "dsv4p",  # haiku tier → dsv4p
    "claude-3-opus-20240229": "glm5.1",

    # OpenAI-style alias names for other agents (no suffix = default _cc format)
    "gpt-4o": "glm5.1",
    "gpt-4o-mini": "dsv4p",
    "o3": "glm5.1",
    "o3-mini": "dsv4p",
    "o4-mini": "dsv4p",
    "gpt-4.1": "glm5.1",
    "gpt-4.1-mini": "dsv4p",
    "gpt-4.1-nano": "dsv4p",
    "codex-mini-latest": "glm5.1",
}

# Thinking support per backend model
# glm5.1 supports reasoning_effort + thinking_budget (ModelScope GLM-5.1 feature)
# dsv4p does NOT support reasoning_effort → proxy must NOT send thinking params to dsv4p
THINKING_SUPPORT = {"glm5.1": True, "dsv4p": False}
DEFAULT_MODEL = "glm5.1"

# ─── Input token safety limits ───────────────────────────────────────────
# ModelScope GLM-5.1 and DSv4P actual API input token limit is 202745
# (confirmed by ModelScope error: "Range of input length should be [1, 202745]").
# MODEL_INPUT_TOKEN_SAFETY is used for reporting context_window to CC via
# /v1/models endpoint. This tells CC the effective capacity, so CC's built-in
# auto-compact triggers at the right time.
# Proxy no longer truncates/compacts messages — that's CC's job exclusively.
MODEL_MAX_INPUT_TOKENS = {"glm5.1": 202745, "dsv4p": 202745}
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.1": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_GLM51", "128000")),
    "dsv4p": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_DSV4P", "128000")),
}

# ─── Thinking config ─────────────────────────────────────────────────────
OUTPUT_TOKEN_MARGIN = 8192  # Room for output after thinking_budget
THINKING_SIGNATURE_DEFAULT = "ErUB3WY0k2GCM2h+4O0S3Y3W3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f"

# ─── Variant×Key 2D round-robin (R21) ─────────────────────────────────────
# 2D round-robin: request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
# → model name: "{base}v{V}k{K}" (e.g. glm5.1v1k1, dsv4pv3k5)
# On 429: same variant, cycle to next key (k→k+1). All 7 keys 429 → return 429 to agent.
# R19 was key-only round-robin (glm5.1k1~k7). R21 adds variant dimension for precise control.
NUM_KEYS = int(os.environ.get("NUM_KEYS", "7"))
NUM_VARIANTS_GLM51 = int(os.environ.get("NUM_VARIANTS_GLM51", "10"))
NUM_VARIANTS_DSV4P = int(os.environ.get("NUM_VARIANTS_DSV4P", "10"))
NUM_VARIANTS = {"glm5.1": NUM_VARIANTS_GLM51, "dsv4p": NUM_VARIANTS_DSV4P}

# Variant model IDs for each backend — proxy uses these to construct precise model names.
# Each variant has independent 200/id/day quota on ModelScope. NEVER remove variants.
# R21: dsv4p reduced from 11→10 variants per user decision (v11 'DeEpSeek-V4-Pro' removed).
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
DSV4P_VARIANT_IDS = [
    "deepseek-ai/deepseek-v4-pro",      # v1
    "deepseek-ai/Deepseek-V4-Pro",      # v2
    "deepseek-ai/DeepSeek-v4-pro",      # v3
    "deepseek-ai/DeepSeek-v4-Pro",      # v4
    "deepseek-ai/DeepSeek-V4-PrO",      # v5
    "deepseek-ai/DeepSeek-V4-PRo",      # v6
    "deepseek-ai/DeepSeeK-V4-Pro",      # v7
    "deepseek-ai/DeepSeEk-V4-Pro",      # v8
    "deepseek-ai/DeepSEek-V4-Pro",      # v9
    "deepseek-ai/DeePSeek-V4-Pro",      # v10
]
VARIANT_IDS = {"glm5.1": GLM51_VARIANT_IDS, "dsv4p": DSV4P_VARIANT_IDS}

_vk_rr_counter = {}  # model → int counter (0..∞), e.g. {"glm5.1": 0, "dsv4p": 0}
_vk_rr_lock = threading.Lock()

def _next_variant_key_pair(model: str) -> tuple:
    """Get next (variant_idx, key_idx) for 2D round-robin.
    Returns 0-based indices. variant_idx in [0, NUM_VARIANTS-1], key_idx in [0, NUM_KEYS-1].
    Counter N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
    """
    num_variants = NUM_VARIANTS.get(model, 10)
    with _vk_rr_lock:
        counter = _vk_rr_counter.get(model, 0)
        variant_idx = (counter // NUM_KEYS) % num_variants
        key_idx = counter % NUM_KEYS
        _vk_rr_counter[model] = counter + 1
        return (variant_idx, key_idx)

def _is_routing_name(name: str) -> bool:
    """Check if a model name is an internal variant×key routing name (e.g. 'glm5.1v1k1', 'dsv4pv3k5').
    R21: Routing names use v+k format. These are proxy→LiteLLM routing, NOT meant for CC/agents.
    Also checks old R19 format (glm5.1k1, dsv4pk3) for backward compatibility."""
    for base in MODEL_UPSTREAMS:
        num_variants = NUM_VARIANTS.get(base, 10)
        # R21 format: base + v{N} + k{K}
        for vi in range(num_variants):
            for ki in range(NUM_KEYS):
                if name == f"{base}v{vi+1}k{ki+1}":
                    return True
        # R19 backward compat: base + k{K}
        for ki in range(NUM_KEYS):
            if name == f"{base}k{ki+1}":
                return True
    return False

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()