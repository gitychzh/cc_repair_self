#!/usr/bin/env python3
"""Configuration constants and environment variables.

All configurable parameters are read from env vars with defaults.
Immutable constraints (variant model IDs, rpm=1, frontend model names,
container names, port assignments) are documented in CLAUDE.md.
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
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_GLM51", "http://glm5.1_test41003:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_GLM51", "http://glm5.1_test41003:4000/v1/models"), "/v1/models"),
    },
    "dsv4p": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/models"), "/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = "glm5.1"

# ─── Model name → LiteLLM model_name mapping ────────────────────────────
# NEVER change the variant model IDs — each has independent 200/id/day quota.
# Tier-based routing (inspired by cc-switch): Claude model tiers → backend models.
# "opus" tier → glm5.1 (high-capability, 7000 dep pool, with thinking)
# "sonnet" tier → glm5.1 (same backend, without thinking for simpler tasks)
# "haiku" tier → dsv4p (lighter model, 77 dep pool, fast responses)
# This allows other agents (OpenCode, Codex, etc.) to specify claude-tier names
# or OpenAI-style names and get routed to appropriate backends automatically.
MODEL_MAP = {
    # Our own model names (direct)
    "glm5.1": "glm5.1", "glm-5.1": "glm5.1", "zhipuai/glm-5.1": "glm5.1",
    "dsv4p": "dsv4p", "deepseek-v4-pro": "dsv4p", "deepseek-ai/deepseek-v4-pro": "dsv4p",
    # Claude Code names → glm5.1 (with and without date suffixes)
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
    # OpenAI-style names for other agents (OpenCode, Codex, etc.)
    # opus-tier equivalent → glm5.1
    "gpt-4o": "glm5.1",
    "gpt-4o-mini": "dsv4p",      # mini tier → dsv4p (lighter)
    "o3": "glm5.1",
    "o3-mini": "dsv4p",
    "o4-mini": "dsv4p",
    "gpt-4.1": "glm5.1",
    "gpt-4.1-mini": "dsv4p",
    "gpt-4.1-nano": "dsv4p",
    "codex-mini-latest": "glm5.1",  # Codex CLI default → glm5.1 (strong coding)
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

# ─── Key round-robin ──────────────────────────────────────────────────────
NUM_KEYS = int(os.environ.get("NUM_KEYS", "7"))
_key_rr_counter = {}
_key_rr_lock = threading.Lock()

def _next_key_idx(model: str) -> int:
    with _key_rr_lock:
        idx = _key_rr_counter.get(model, 0)
        _key_rr_counter[model] = (idx + 1) % NUM_KEYS
        return idx

def _is_key_group_name(name: str) -> bool:
    for base in MODEL_UPSTREAMS:
        for ki in range(NUM_KEYS):
            if name == f"{base}k{ki+1}":
                return True
    return False

# ─── Thread locks for logging ────────────────────────────────────────────
_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()