#!/usr/bin/env python3
"""Configuration constants and environment variables.

All configurable parameters are read from env vars with defaults.
Immutable constraints (variant model IDs, rpm=1, frontend model names,
container names, port assignments) are documented in CLAUDE.md.

R21: Added NUM_VARIANTS, VARIANT_IDS, v×k 2D round-robin support.
R23: Added AGENT_SUFFIXES, agent type detection, suffix-based model IDs.
R29: Added PROXY_ROLE, dsv4p backend routing, removed LiteLLM fallback.
Proxy precisely specifies variant+key combo → LiteLLM just forwards.
"""
import os
import threading

# ─── Network ──────────────────────────────────────────────────────────────
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-local")
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "40001"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "300"))  # Overall request timeout concept (for docs)
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "60"))  # R27: Per-key HTTPConnection timeout, separated from PROXY_TIMEOUT

# ─── Proxy Role (R29) ────────────────────────────────────────────────────
# Each proxy container serves a specific role:
#   "cc"          → only /v1/messages (Anthropic format, CC), upstream=glm5.2
#   "codex"       → only /v1/responses (Responses API, Codex), upstream=glm5.2
#   "passthrough" → only /v1/chat/completions (OpenAI format, _ol/_oc/_hm), upstream=dsv4p
# This determines which endpoints to serve and which backend model to default to.
PROXY_ROLE = os.environ.get("PROXY_ROLE", "cc")

# ─── Role-based defaults ──────────────────────────────────────────────────
# Default upstream model based on role:
#   cc/codex → glm5.2 (CC and Codex need Anthropic/Responses format conversion)
#   passthrough → dsv4p (OpenAI agents get nearly-transparent passthrough)
ROLE_DEFAULT_UPSTREAM = {
    "cc": "glm5.2",
    "codex": "glm5.2",
    "passthrough": "dsv4p",
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
# R29: Two backend models (glm5.2 and dsv4p), both routed through ms_uni41001.
# LiteLLM fallback removed — single LiteLLM container only.
# Each proxy container uses its PROXY_ROLE to determine which backend to use.
MODEL_UPSTREAMS = {
    "glm5.2": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_GLM51", "http://ms_uni41001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_GLM51", "http://ms_uni41001:4000/v1/models"), "/v1/models"),
    },
    "dsv4p": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_DSV4P", "http://ms_uni41001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_DSV4P", "http://ms_uni41001:4000/v1/models"), "/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = ROLE_DEFAULT_UPSTREAM.get(PROXY_ROLE, "glm5.2")

# ─── Agent type suffixes (R23, R29 update) ────────────────────────────────
# Suffix determines: 1) Response format (anthropic/openai/responses)  2) Backend model  3) Error format
# R29: _ol/_oc/_hm now route to dsv4p backend (separate proxy 40003)
# "_cc" → Anthropic format, backend=glm5.2 (CC only, proxy 40001)
# "_cx" → Responses API format, backend=glm5.2 (Codex only, proxy 40002)
# "_ol/_oc/_hm" → OpenAI format, backend=dsv4p (OpenAI agents, proxy 40003)
AGENT_SUFFIXES = {
    "_cc": {"name": "Claude Code", "format": "anthropic", "backend": "glm5.2"},
    "_ol": {"name": "OpenClaw",    "format": "openai",    "backend": "dsv4p"},
    "_oc": {"name": "OpenCode",    "format": "openai",    "backend": "dsv4p"},
    "_hm": {"name": "Hermes",      "format": "openai",    "backend": "dsv4p"},
    "_cx": {"name": "Codex",       "format": "responses", "backend": "glm5.2"},
}
DEFAULT_AGENT_SUFFIX = "_cc"  # backward compat: no suffix = CC (Anthropic format)

# Base model names (backend routing targets)
BASE_MODELS = ["glm5.2", "dsv4p"]

def detect_agent_type(model_id):
    """Detect agent type from model ID suffix.

    Args:
        model_id: model name, e.g. "glm5.2_cc", "dsv4p_ol", "glm5.2", "claude-opus-4-8"

    Returns:
        (base_model, agent_suffix, response_format)
        base_model: backend model name ("glm5.2" or "dsv4p")
        agent_suffix: "_cc", "_ol", "_oc", "_hm" or DEFAULT_AGENT_SUFFIX
        response_format: "anthropic", "openai" or "responses"

    Examples:
        "glm5.2_cc" → ("glm5.2", "_cc", "anthropic")
        "dsv4p_ol"  → ("dsv4p", "_ol", "openai")
        "glm5.2"    → ("glm5.2", "_cc", "anthropic")  # backward compat
        "claude-opus-4-8" → ("glm5.2", "_cc", "anthropic")  # MODEL_MAP lookup
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
    # Try MODEL_MAP lookup first (e.g. "claude-opus-4-8" → "glm5.2")
    mapped = MODEL_MAP.get(model_id, None)
    if mapped and mapped in MODEL_UPSTREAMS:
        return (mapped, DEFAULT_AGENT_SUFFIX, AGENT_SUFFIXES[DEFAULT_AGENT_SUFFIX]["format"])

    # Direct backend model name (e.g. "glm5.2")
    if model_id in MODEL_UPSTREAMS:
        return (model_id, DEFAULT_AGENT_SUFFIX, AGENT_SUFFIXES[DEFAULT_AGENT_SUFFIX]["format"])

    # Unknown model → default based on PROXY_ROLE
    return (DEFAULT_UPSTREAM_MODEL, DEFAULT_AGENT_SUFFIX, AGENT_SUFFIXES[DEFAULT_AGENT_SUFFIX]["format"])

def format_model_id(base_model, agent_suffix):
    """Construct frontend model ID from base model + agent suffix.
    e.g. ("glm5.2", "_cc") → "glm5.2_cc", ("dsv4p", "_ol") → "dsv4p_ol"
    """
    return f"{base_model}{agent_suffix}"

# ─── Model name → LiteLLM model_name mapping ────────────────────────────
# NEVER change the variant model IDs — each has independent 200/id/day quota.
# R29: _ol/_oc/_hm route to dsv4p backend, _cc/_cx route to glm5.2 backend.
MODEL_MAP = {
    # ─── glm5.2 backend (_cc Anthropic, _cx Responses) ───
    # Claude Code (_cc) — Anthropic format, backend=glm5.2
    "glm5.2_cc": "glm5.2",
    # Codex (_cx) — Responses API format, backend=glm5.2
    "glm5.2_cx": "glm5.2",

    # ─── dsv4p backend (_ol/_oc/_hm OpenAI) ───
    # OpenClaw (_ol) — OpenAI format, backend=dsv4p
    "dsv4p_ol": "dsv4p",
    # OpenCode (_oc) — OpenAI format, backend=dsv4p
    "dsv4p_oc": "dsv4p",
    # Hermes (_hm) — OpenAI format, backend=dsv4p
    "dsv4p_hm": "dsv4p",
    # Backward compat: old suffix with glm5.2 base → still routes to dsv4p
    "glm5.2_ol": "dsv4p",
    "glm5.2_oc": "dsv4p",
    "glm5.2_hm": "dsv4p",

    # ─── Backward compat: no suffix = CC (Anthropic format) ───
    "glm5.2": "glm5.2", "glm-5.2": "glm5.2", "zhipuai/glm-5.2": "glm5.2",
    "dsv4p": "dsv4p", "deepseek-v4-pro": "dsv4p",

    # Claude Code names → glm5.2 (CC, implicitly _cc / Anthropic format)
    "claude-opus-4-8": "glm5.2",
    "claude-opus-4-7": "glm5.2",
    "claude-opus-4": "glm5.2",
    "claude-sonnet-4-6": "glm5.2",
    "claude-sonnet-4": "glm5.2",
    "claude-haiku-4-5": "glm5.2",
    "claude-sonnet-4-20250514": "glm5.2",
    "claude-sonnet-4-6-20250514": "glm5.2",
    "claude-opus-4-20250514": "glm5.2",
    "claude-opus-4-8-20250514": "glm5.2",
    "claude-haiku-4-5-20251001": "glm5.2",
    "claude-3-5-sonnet-20241022": "glm5.2",
    "claude-3-5-haiku-20241022": "glm5.2",
    "claude-3-opus-20240229": "glm5.2",

    # OpenAI-style alias names → dsv4p (for passthrough proxy, OpenAI format)
    "gpt-4o": "dsv4p",
    "gpt-4o-mini": "dsv4p",
    "o3": "dsv4p",
    "o3-mini": "dsv4p",
    "o4-mini": "dsv4p",
    "gpt-4.1": "dsv4p",
    "gpt-4.1-mini": "dsv4p",
    "gpt-4.1-nano": "dsv4p",
    # Codex CLI alias → glm5.2 (Codex专用, Responses API format)
    "codex-mini-latest": "glm5.2",
}

# Thinking support per backend model
# glm5.2 supports reasoning_effort + thinking_budget (ModelScope GLM-5.2 feature)
# dsv4p does NOT support thinking_budget (DSv4P没有thinking参数)
THINKING_SUPPORT = {"glm5.2": True, "dsv4p": False}
DEFAULT_MODEL = ROLE_DEFAULT_UPSTREAM.get(PROXY_ROLE, "glm5.2")

# ─── Input token safety limits ───────────────────────────────────────────
# ModelScope GLM-5.2 actual API input token limit is 202745
# DSv4P context window — using conservative estimate (128K), will verify later
MODEL_MAX_INPUT_TOKENS = {
    "glm5.2": 202745,
    "dsv4p": 128000,
}
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.2": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_GLM51", "170000")),
    "dsv4p": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_DSV4P", "128000")),
}

# ─── Thinking config ─────────────────────────────────────────────────────
OUTPUT_TOKEN_MARGIN = 8192  # Room for output after thinking_budget
THINKING_SIGNATURE_DEFAULT = "ErUB3WY0k2GCM2h+4O0S3Y3W3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f"

# ─── Variant×Key 2D round-robin (R21, R29: added dsv4p) ──────────────────
# 2D round-robin: request N → variant_idx=(N//NUM_KEYS)%NUM_VARIANTS, key_idx=N%NUM_KEYS
# → model name: "glm5.2v{V}k{K}" or "dsv4pv{V}k{K}"
# On 429: same variant, cycle to next key (k→k+1). All 7 keys 429 → variant fallback (R23)
NUM_KEYS = int(os.environ.get("NUM_KEYS", "7"))
NUM_VARIANTS_GLM51 = int(os.environ.get("NUM_VARIANTS_GLM51", "10"))
NUM_VARIANTS_DSV4P = int(os.environ.get("NUM_VARIANTS_DSV4P", "10"))
NUM_VARIANTS = {"glm5.2": NUM_VARIANTS_GLM51, "dsv4p": NUM_VARIANTS_DSV4P}

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
# R29: Restored dsv4p variant IDs (10 variants, independent 200/id/day quota each)
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
VARIANT_IDS = {"glm5.2": GLM51_VARIANT_IDS, "dsv4p": DSV4P_VARIANT_IDS}

_vk_rr_counter = {}  # model → int counter (0..∞), e.g. {"glm5.2": 0, "dsv4p": 0}
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
    """Check if a model name is an internal variant×key routing name.
    R21: Routing names use v+k format. These are proxy→LiteLLM routing, NOT meant for agents.
    Checks for both glm5.2 and dsv4p routing names."""
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
