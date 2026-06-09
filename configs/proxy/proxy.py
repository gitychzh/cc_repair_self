#!/usr/bin/env python3
"""Anthropic ↔ OpenAI format converter proxy with metrics logging.

Format conversion only — no proxy-level retry. All retry/fallback/routing
delegated to LiteLLM upstream.

Architecture:
  CC(40001) → this proxy (format conversion + metrics + input safety)
      → 41001 LiteLLM (glm5.1, with retry/fallback/routing)
      → 42001 LiteLLM (dsv4p, with retry/fallback/routing)

Env vars:
  LITELLM_URL_GLM51  — glm5.1 chat URL (default: http://glm5.1_uni41001:4000/v1/chat/completions)
  LITELLM_URL_DSV4P  — dsv4p chat URL (default: http://dsv4p_uni42001:4000/v1/chat/completions)
  LITELLM_MODELS_URL_GLM51 — glm5.1 models URL
  LITELLM_MODELS_URL_DSV4P — dsv4p models URL
  LITELLM_KEY        — upstream API key (default: sk-litellm-local)
  LISTEN_PORT         — listen port (default: 40001)
  PROXY_TIMEOUT       — upstream timeout seconds (default: 300)
  MAX_TOOL_DESC       — max chars for tool descriptions (default: 2000)
  MAX_SCHEMA_DESC     — max chars for schema param descriptions (default: 600)
  CHARS_PER_TOKEN_ESTIMATE — chars per token for input safety (default: 2.0, mixed Chinese/English)
  MODEL_INPUT_TOKEN_SAFETY_GLM51 — glm5.1 input token safety limit (default: 128000, model capacity 131072)
  MODEL_INPUT_TOKEN_SAFETY_DSV4P  — dsv4p input token safety limit (default: 128000)
  LOG_DIR             — log directory (default: /app/logs)
"""
import http.server
import json
import os
import sys
import time
import datetime
import threading
import http.client
import urllib.parse
import socketserver
import socket
import re
import uuid

# ─── Configuration ────────────────────────────────────────────────────────
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-local")
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "40001"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "300"))
MAX_TOOL_DESC = int(os.environ.get("MAX_TOOL_DESC", "2000"))
MAX_SCHEMA_DESC = int(os.environ.get("MAX_SCHEMA_DESC", "600"))
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("CHARS_PER_TOKEN_ESTIMATE", "2.0"))

def _ensure_url_path(url: str, path: str) -> str:
    """If env var provides only host or host/v1, append the required full path."""
    stripped = url.rstrip("/")
    if stripped.endswith(path):
        return url
    if stripped.endswith("/v1"):
        return stripped + path.replace("/v1", "", 1)
    return stripped + path

# Per-model upstream routing — chat_url and models_url
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_GLM51", "http://glm5.1_uni41001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_GLM51", "http://glm5.1_uni41001:4000/v1/models"), "/v1/models"),
    },
    "dsv4p": {
        "chat_url": _ensure_url_path(os.environ.get("LITELLM_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/chat/completions"), "/v1/chat/completions"),
        "models_url": _ensure_url_path(os.environ.get("LITELLM_MODELS_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/models"), "/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = "glm5.1"

# Model name → LiteLLM model_name mapping
MODEL_MAP = {
    "glm5.1": "glm5.1", "glm-5.1": "glm5.1", "zhipuai/glm-5.1": "glm5.1",
    "dsv4p": "dsv4p", "deepseek-v4-pro": "dsv4p", "deepseek-ai/deepseek-v4-pro": "dsv4p",
    # Claude Code names → glm5.1 (with and without date suffixes)
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
}

# Input token safety limits — read from env vars, fallback to 128000
# docker-compose passes MODEL_INPUT_TOKEN_SAFETY_GLM51/DSV4P env vars.
# ModelScope GLM-5.1 and DSv4P actual API input token limit is 202745 (confirmed by
# ModelScope error: "Range of input length should be [1, 202745]").
# MODEL_INPUT_TOKEN_SAFETY is used for reporting context_window to CC via
# /v1/models endpoint. This tells CC the effective capacity, so CC's built-in
# auto-compact (settings.json autoCompactWindow) triggers at the right time.
# Proxy no longer truncates/compacts messages — that's CC's job exclusively.
MODEL_MAX_INPUT_TOKENS = {"glm5.1": 202745, "dsv4p": 202745}
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.1": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_GLM51", "128000")),
    "dsv4p": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_DSV4P", "128000")),
}

DEFAULT_MODEL = "glm5.1"

_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()

def _log(level, msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:10]
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date = datetime.date.today().isoformat()
        with _log_lock, open(os.path.join(LOG_DIR, f"proxy.{date}.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _log_metrics(entry):
    """Write structured JSON metrics to metrics.{date}.jsonl for optimization analysis."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date = datetime.date.today().isoformat()
        with _metrics_lock, open(os.path.join(LOG_DIR, f"metrics.{date}.jsonl"), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass

def _log_error_detail(detail):
    """Write detailed error info to error_detail.{date}.jsonl for root-cause analysis."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        date = datetime.date.today().isoformat()
        with _error_detail_lock, open(os.path.join(LOG_DIR, f"error_detail.{date}.jsonl"), "a") as f:
            f.write(json.dumps(detail, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass

# ─── Truncation (ModelScope requires short descriptions) ──────────────────

def _truncate_desc(text, max_len):
    if not text or len(text) <= max_len:
        return text
    double_nl = text.find("\n\n")
    if double_nl > 0 and double_nl <= max_len * 2:
        result = text[:double_nl].strip()
        if len(result) <= max_len:
            return result
    truncated = text[:max_len]
    last_sentence = truncated.rfind(". ")
    if last_sentence > max_len // 4:
        return text[:last_sentence + 1].strip()
    return text[:max_len - 3].rstrip() + "..."

def _truncate_schema_descriptions(schema, max_len=MAX_SCHEMA_DESC):
    if isinstance(schema, dict):
        for key in schema:
            if key == "description" and isinstance(schema[key], str):
                schema[key] = _truncate_desc(schema[key], max_len)
            else:
                _truncate_schema_descriptions(schema[key], max_len)
    elif isinstance(schema, list):
        for item in schema:
            _truncate_schema_descriptions(item, max_len)
    return schema

# ─── Text content character estimation for INPUT-REJECT ─────────────────────
# R9 fix: Previously total_input_chars = len(json.dumps(oai_body)), which included
# JSON structure overhead (brackets, keys, formatting). This caused estimated_tokens
# to be wildly inaccurate — JSON keys like "type", "function" are 1-2 tokens each
# but their character representation inflates the char count by 200-300%.
# The fix: only count actual text content (message text, system prompt, tool names/
# descriptions/parameter descriptions). This gives a much more accurate estimate.
# Example: a request with 80K actual tokens had json.dumps ≈ 350K chars →
#   estimated = 350K/3.5 = 100K (25% overestimate) → borderline INPUT-REJECT.
# With text-only: text_chars ≈ 120K → estimated = 120K/2.0 = 60K (25% underestimate,
# but safely below 120K limit). The underestimate is intentional — we only reject
# requests that are genuinely oversized, not borderline ones that ModelScope can handle.

def _estimate_text_chars(oai_body):
    """Estimate character count of actual text content in an OpenAI-format request body.
    Only counts text that would actually be tokenized — excludes JSON structure overhead.
    """
    text_chars = 0

    # System prompt (OpenAI format: {"role": "system", "content": "..."})
    for msg in oai_body.get("messages", []):
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                text_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_chars += len(block.get("text", ""))
                    elif isinstance(block, str):
                        text_chars += len(block)

    # User and assistant messages
    for msg in oai_body.get("messages", []):
        role = msg.get("role", "")
        if role == "system":
            continue  # Already counted above
        content = msg.get("content", "")
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text_chars += len(block.get("text", ""))
                    elif block_type == "thinking":
                        text_chars += len(block.get("thinking", ""))
                    elif block_type == "tool_use":
                        # Tool call input (arguments JSON) — these ARE tokenized
                        text_chars += len(json.dumps(block.get("input", {})))
                        text_chars += len(block.get("name", ""))
                    elif block_type == "tool_result":
                        # Tool result content
                        tc = block.get("content", "")
                        if isinstance(tc, str):
                            text_chars += len(tc)
                        elif isinstance(tc, list):
                            for sub_block in tc:
                                if isinstance(sub_block, dict) and sub_block.get("type") == "text":
                                    text_chars += len(sub_block.get("text", ""))
                                else:
                                    text_chars += len(json.dumps(sub_block, default=str))
                    elif block_type == "image_url":
                        # Image URLs are tokenized but differently — rough estimate
                        url = block.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # Base64 image — roughly 1000 tokens per image regardless of size
                            text_chars += 8000  # Approximate token cost of a typical image
                        else:
                            text_chars += len(url)
                elif isinstance(block, str):
                    text_chars += len(block)

        # Tool calls in assistant messages (already in content list above,
        # but also check the tool_calls key for OpenAI format)
        tool_calls = msg.get("tool_calls", [])
        if tool_calls and isinstance(content, str):  # Only if content wasn't a list
            for tc in tool_calls:
                fn = tc.get("function", {})
                text_chars += len(fn.get("name", ""))
                text_chars += len(fn.get("arguments", ""))

    # Tool definitions (these ARE tokenized as part of the system context)
    for tool in oai_body.get("tools", []):
        fn = tool.get("function", {})
        text_chars += len(fn.get("name", ""))
        text_chars += len(fn.get("description", ""))
        # Parameter schema descriptions are tokenized too
        text_chars += len(json.dumps(fn.get("parameters", {})))

    return text_chars

# ─── Anthropic → OpenAI Format Conversion ──────────────────────────────────

def _tool_anth_to_oai(anth_tools):
    oai_tools = []
    for tool in anth_tools:
        if tool.get("type", "tool_use") != "tool_use":
            continue
        name = tool.get("name", "")
        desc = _truncate_desc(tool.get("description", ""), MAX_TOOL_DESC)
        schema = _truncate_schema_descriptions(tool.get("input_schema", {}))
        oai_tools.append({
            "type": "function",
            "function": {"name": name, "description": desc, "parameters": schema},
        })
    return oai_tools

def _convert_tool_choice(anth_choice):
    if not anth_choice:
        return None
    if isinstance(anth_choice, dict):
        ctype = anth_choice.get("type", "")
        if ctype == "auto":
            return "auto"
        if ctype == "none":
            return "none"
        if ctype == "any":
            return "required"
        if ctype == "tool":
            return {"type": "function", "function": {"name": anth_choice.get("name", "")}}
    if isinstance(anth_choice, str):
        return anth_choice
    return None

def anth_to_openai(body, target_model=None):
    model = target_model or body.get("model", "glm5.1")
    system_text = ""
    system_blocks = body.get("system")
    if system_blocks:
        if isinstance(system_blocks, str):
            system_text = system_blocks
        elif isinstance(system_blocks, list):
            parts = []
            for block in system_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            system_text = "\n".join(parts)

    oai_messages = []
    if system_text:
        oai_messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, list):
                text_parts = []
                tool_results = []
                image_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            tool_results.append(block)
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "image":
                            # Convert Anthropic image block to OpenAI image_url format
                            source = block.get("source", {})
                            if source.get("type") == "base64":
                                media_type = source.get("media_type", "image/png")
                                data = source.get("data", "")
                                image_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                                })
                            elif source.get("type") == "url":
                                image_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": source.get("url", "")},
                                })
                    elif isinstance(block, str):
                        text_parts.append(block)
                # Build OpenAI content array: text + images together in multimodal format
                if image_parts:
                    # Multimodal content — must use content array format for OpenAI
                    oai_content = []
                    for tp in text_parts:
                        oai_content.append({"type": "text", "text": tp})
                    for ip in image_parts:
                        oai_content.append(ip)
                    oai_messages.append({"role": "user", "content": oai_content})
                elif text_parts:
                    # Simple text-only message — use string content
                    oai_messages.append({"role": "user", "content": "\n".join(text_parts)})
                for tr in tool_results:
                    tool_id = tr.get("tool_use_id", "")
                    content_str = ""
                    tc = tr.get("content", "")
                    if isinstance(tc, str):
                        content_str = tc
                    elif isinstance(tc, list):
                        parts = []
                        for b in tc:
                            if isinstance(b, dict) and b.get("type") == "text":
                                parts.append(b.get("text", ""))
                            else:
                                parts.append(json.dumps(b, default=str))
                        content_str = "\n".join(parts)
                    oai_messages.append({"role": "tool", "tool_call_id": tool_id, "content": content_str})
            elif isinstance(content, str):
                oai_messages.append({"role": "user", "content": content})
            else:
                oai_messages.append({"role": "user", "content": str(content)})

        elif role == "assistant":
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                msg_dict = {"role": "assistant"}
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                else:
                    msg_dict["content"] = None
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                oai_messages.append(msg_dict)
            elif isinstance(content, str):
                oai_messages.append({"role": "assistant", "content": content})
            else:
                oai_messages.append({"role": "assistant", "content": str(content)})

        elif role == "tool":
            # Already handled in user tool_result
            pass

    oai_body = {
        "model": model,
        "messages": oai_messages,
        "stream": body.get("stream", False),
        # Request usage data in streaming chunks so we can report real token counts
        # to Claude Code CLI (otherwise TUI shows 0/200000 tokens)
        "stream_options": {"include_usage": True},
    }
    # Anthropic uses max_tokens for output limit; newer versions also accept max_completion_tokens
    # OpenAI uses max_completion_tokens (new) or max_tokens (legacy) for output limit
    # Prefer max_completion_tokens if set, fall back to max_tokens
    output_tokens = body.get("max_completion_tokens") or body.get("max_tokens", 4096)
    if output_tokens:
        oai_body["max_tokens"] = output_tokens
        oai_body["max_completion_tokens"] = output_tokens
    if body.get("temperature"):
        oai_body["temperature"] = body["temperature"]
    if body.get("top_p"):
        oai_body["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        oai_body["stop"] = body["stop_sequences"]
    if body.get("tools"):
        oai_tools = _tool_anth_to_oai(body["tools"])
        if oai_tools:
            oai_body["tools"] = oai_tools
    tc = _convert_tool_choice(body.get("tool_choice"))
    if tc:
        oai_body["tool_choice"] = tc

    # Anthropic thinking → GLM-5.1 thinking_budget + reasoning_effort
    # ModelScope GLM-5.1 requires: max_completion_tokens > thinking_budget
    # Claude Code sends thinking.budget_tokens=32768 (default) with max_tokens=8192
    # We must ensure max_completion_tokens > thinking_budget
    # NOTE: DSv4P does NOT support reasoning_effort — only set it for glm5.1
    if body.get("thinking"):
        thinking_cfg = body["thinking"]
        budget = thinking_cfg.get("budget_tokens", 8000)
        # Pass thinking_budget directly for ModelScope GLM-5.1
        oai_body["thinking_budget"] = budget
        # Ensure max_completion_tokens > thinking_budget (ModelScope constraint)
        # Leave room for actual output after thinking: thinking_budget + output margin
        OUTPUT_TOKEN_MARGIN = 8192
        required_min = budget + OUTPUT_TOKEN_MARGIN
        if output_tokens < required_min:
            output_tokens = required_min
            oai_body["max_tokens"] = output_tokens
            oai_body["max_completion_tokens"] = output_tokens
        # Set reasoning_effort for GLM-5.1 only — DSv4P doesn't support it
        if target_model == "glm5.1":
            if budget >= 10000:
                oai_body["reasoning_effort"] = "high"
            elif budget >= 5000:
                oai_body["reasoning_effort"] = "medium"
            else:
                oai_body["reasoning_effort"] = "low"

    return oai_body

# ─── OpenAI → Anthropic Format Conversion ──────────────────────────────────

def openai_to_anth(oai_response, request_model):
    content = []
    oai_content = oai_response.get("choices", [])
    if not oai_content:
        return {"type": "message", "role": "assistant", "content": [{"type": "text", "text": ""}],
                "model": request_model, "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}

    choice = oai_content[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")

    # Thinking/reasoning content → Anthropic thinking block
    reasoning = message.get("reasoning_content", "")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning,
                        "signature": os.environ.get("THINKING_SIGNATURE", "ErUB3WY0k2GCM2h+4O0S3Y3W3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f")})

    # Text content
    text = message.get("content", "")
    if text:
        content.append({"type": "text", "text": text})

    # Tool calls
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            input_data = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            input_data = {"raw": fn.get("arguments", "")}
        content.append({"type": "tool_use", "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                        "name": fn.get("name", ""), "input": input_data})

    if not content:
        content.append({"type": "text", "text": ""})

    stop_reason = "end_turn"
    if finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "tool_calls":
        stop_reason = "tool_use"

    usage = oai_response.get("usage", {})
    return {
        "id": oai_response.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "model": request_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }

# ─── Proxy Handler ────────────────────────────────────────────────────────

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/"):
            gw_urls = {k: v["chat_url"] for k, v in MODEL_UPSTREAMS.items()}
            self._send_json(200, {
                "status": "ok",
                "proxy": "anthropic-to-openai",
                "gateways": gw_urls,
                "port": LISTEN_PORT,
            })
        elif parsed.path == "/v1/models" or parsed.path == "/models":
            # Check if this is a Anthropic-format request (has anthropic-version header)
            anth_version = self.headers.get("anthropic-version")
            if anth_version:
                self._anthropic_models_list()
            else:
                self._proxy_models()
        elif parsed.path.startswith("/v1/models/") or parsed.path.startswith("/models/"):
            # Anthropic model detail endpoint: /v1/models/{model_id}
            model_id = parsed.path.split("/models/")[1].strip("/")
            self._anthropic_model_detail(model_id)
        else:
            self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/", "/v1/models", "/models") or parsed.path.startswith("/v1/models/") or parsed.path.startswith("/models/"):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/messages":
            self._handle_messages()
        elif parsed.path in ("/v1/chat/completions", "/chat/completions"):
            self._passthrough_openai()
        else:
            self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ─── /v1/messages — Anthropic format request ───
    def _handle_messages(self):
        t_start = time.time()
        request_id = str(uuid.uuid4())[:8]
        metrics = {
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "path": "/v1/messages",
            "request_model": "?",
            "mapped_model": "?",
            "stream": False,
            "num_messages": 0,
            "num_tools": 0,
            "system_prompt_chars": 0,
            "total_input_chars": 0,
            "ttfb_ms": None,
            "duration_ms": 0,
            "status": 0,
            "finish_reason": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "tool_truncation": None,
            "error_type": None,
            "error_message": None,
            "upstream": "?",
        }

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            anth_body = json.loads(raw_body)
        except Exception as e:
            self._send_json(400, {"error": {"message": f"bad request: {e}"}})
            metrics["status"] = 400; metrics["error_type"] = "BadRequest"; metrics["error_message"] = str(e)
            _log("ERROR", f"bad request: {e}")
            _log_metrics(metrics)
            return

        request_model = anth_body.get("model", DEFAULT_MODEL)
        is_stream = anth_body.get("stream", False)
        metrics["request_model"] = request_model
        metrics["stream"] = is_stream

        # Track system prompt size
        system_blocks = anth_body.get("system")
        if system_blocks:
            if isinstance(system_blocks, str):
                metrics["system_prompt_chars"] = len(system_blocks)
            elif isinstance(system_blocks, list):
                metrics["system_prompt_chars"] = sum(
                    len(b.get("text", "")) if isinstance(b, dict) else len(b)
                    for b in system_blocks
                )

        # Convert Anthropic → OpenAI (map Claude model names to LiteLLM model_name)
        request_model_raw = request_model
        mapped_model = MODEL_MAP.get(request_model, DEFAULT_MODEL)
        oai_body = anth_to_openai(anth_body, target_model=mapped_model)
        metrics["mapped_model"] = mapped_model
        metrics["num_messages"] = len(oai_body.get("messages", []))
        metrics["num_tools"] = len(oai_body.get("tools", []))
        # R9 fix: Use text-only chars estimation instead of len(json.dumps(oai_body)).
        # json.dumps includes JSON structure overhead (brackets, keys, formatting) that
        # inflates char count by 200-300% vs actual tokenizable text. This caused
        # estimated_tokens to be wildly inaccurate → legitimate requests incorrectly
        # INPUT-REJECTED → "Repeated 529 Overloaded" crash in CC.
        text_chars = _estimate_text_chars(oai_body)
        json_chars = len(json.dumps(oai_body))
        metrics["total_input_chars"] = text_chars  # Text content chars (accurate estimate)
        metrics["total_input_chars_json"] = json_chars  # Full JSON chars (for comparison/debugging)
        metrics["text_vs_json_ratio"] = round(text_chars / json_chars, 2) if json_chars > 0 else 0

        # Track tool truncation
        if metrics["num_tools"] > 0:
            total_orig = sum(len(t.get("description", "")) for t in anth_body.get("tools", [])
                            if t.get("type", "tool_use") == "tool_use")
            total_trunc = sum(len(t.get("function", {}).get("description", ""))
                            for t in oai_body.get("tools", [])
                            if t.get("type") == "function")
            metrics["tool_truncation"] = {
                "original_total_chars": total_orig,
                "truncated_total_chars": total_trunc,
                "reduction_pct": round((1 - total_trunc / total_orig) * 100, 1) if total_orig > 0 else 0,
                "num_tools": metrics["num_tools"],
            }

        # Select upstream
        upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
        upstream = MODEL_UPSTREAMS[upstream_key]
        litellm_url = upstream["chat_url"]
        metrics["upstream"] = upstream_key

        # ─── Input token estimation (metrics only, no proxy-level truncation) ───
        # Proxy no longer auto-compacts/truncates messages. CC's built-in auto-compact
        # (triggered by autoCompactWindow in settings.json) handles compression.
        # Proxy-level truncation caused catastrophic context loss ("completely forgets
        # everything"). Let CC's native mechanism handle it — same outcome quality-wise
        # but at least it's CC's own decision, not a silent brutal truncation.
        # If input truly exceeds ModelScope's 202745 limit, it will fail at ModelScope
        # and we return invalid_request_error → CC stops (no compression loop, no retry).
        estimated_tokens = int(metrics["total_input_chars"] / CHARS_PER_TOKEN_ESTIMATE)
        estimated_tokens_json = int(metrics["total_input_chars_json"] / CHARS_PER_TOKEN_ESTIMATE)
        metrics["estimated_input_tokens"] = estimated_tokens
        metrics["estimated_input_tokens_json"] = estimated_tokens_json
        if estimated_tokens > 120000:
            _log("INPUT-WARN", f"estimated_tokens={estimated_tokens} (json_est={estimated_tokens_json}) — large context, CC auto-compact may trigger soon")

        # ─── ModelScope force-stream ───
        # ModelScope non-stream responses (both GLM-5.1 and DSv4P) intermittently include
        # a 'delta' field in choices[0], which is invalid for OpenAI non-stream format.
        # LiteLLM's response parser crashes on this (choices=None → InternalServerError).
        # Data: glm5.1 non-stream has 14% 500-error rate (18/127), dsv4p is 100% broken.
        # Streaming mode always works. Fix: for ALL non-stream requests, force stream=True
        # to LiteLLM, then collect streaming chunks and synthesize non-stream Anthropic response.
        force_stream_for_nonstream = (not is_stream)
        if force_stream_for_nonstream:
            oai_body["stream"] = True
            _log("FORCE-STREAM", f"non-stream → forcing stream=True (collect+synthesize)")

        _log("REQ", f"model={request_model}→{mapped_model} stream={is_stream} "
                    f"msgs={len(oai_body.get('messages',[]))} "
                    f"tools={len(oai_body.get('tools',[]))}")

        # Forward to LiteLLM (no proxy-level retry — LiteLLM handles all retry/fallback)
        auth_key = self.headers.get("x-api-key") or self.headers.get("X-Api-Key") or LITELLM_KEY
        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_key}",
            "Content-Length": str(len(json.dumps(oai_body).encode("utf-8"))),
        }
        oai_data = json.dumps(oai_body).encode("utf-8")
        parsed_upstream = urllib.parse.urlparse(litellm_url)

        try:
            conn = self._make_upstream_conn(parsed_upstream)
            conn.request("POST", parsed_upstream.path, body=oai_data, headers=headers_out)
            resp = conn.getresponse()

            # Extract LiteLLM routing/quota headers for optimization analytics
            # These headers reveal: which deployment was selected, quota remaining,
            # and actual LLM provider latency — critical for routing strategy optimization.
            for hdr_key, metrics_key in [
                ("x-litellm-model-id", "litellm_model_id"),
                ("x-litellm-response-duration-ms", "litellm_response_duration_ms"),
            ]:
                val = resp.getheader(hdr_key)
                if val:
                    metrics[metrics_key] = val

            # Extract ModelScope quota headers from LiteLLM-passed llm_provider-* headers
            for hdr_key, metrics_key in [
                ("llm_provider-modelscope-ratelimit-model-requests-remaining", "ms_model_requests_remaining"),
                ("llm_provider-modelscope-ratelimit-requests-remaining", "ms_requests_remaining"),
            ]:
                val = resp.getheader(hdr_key)
                if val:
                    metrics[metrics_key] = int(val)

            if resp.status >= 400:
                error_body = resp.read()
                try:
                    error_json = json.loads(error_body)
                except Exception:
                    error_json = {"error": error_body.decode("utf-8", errors="replace")}
                conn.close()

                # Resilience retry for 401/403 AuthenticationError:
                # LiteLLM marks KEY5 deployments as cooldown after 401, but if too many
                # deployments are also marked unhealthy (from health check side effects),
                # LiteLLM's own num_retries may exhaust all healthy options and return 401.
                # Re-sending the same request once forces LiteLLM to re-route (KEY5 now in
                # cooldown → different deployment selected). This is NOT "proxy retry of the
                # same deployment" — it's a re-routing opportunity after cooldown kicks in.
                err_str = json.dumps(error_json)
                should_resilience_retry = (
                    resp.status in (401, 403)
                    and "AuthenticationError" in err_str
                    and metrics.get("_resilience_retry_count", 0) < 1
                )

                # No rate_limit_retry: data shows 8% success rate (1/13) with 2s wait.
                # CC has built-in retry with backoff on rate_limit_error (429 stays as 429).
                # LiteLLM's own num_retries=5 handles deployment rotation on 429.
                # Proxy retry wastes 2s latency for 92% of already-failed requests.
                should_rate_limit_retry = False

                # Resilience retry for InvalidParameter (thinking_budget > max_completion_tokens):
                # ModelScope GLM-5.1 requires max_completion_tokens > thinking_budget.
                # The pre-flight check in anth_to_openai should prevent this, but if an
                # edge case causes it, we fix the parameters and retry once rather than
                # just forwarding the error to CC (which would crash without retry).
                should_fix_thinking_budget = (
                    resp.status == 400
                    and "InvalidParameter" in err_str
                    and "thinking_budget" in err_str
                    and "max_completion_tokens" in err_str
                    and metrics.get("_thinking_budget_retry_count", 0) < 1
                )

                if should_fix_thinking_budget:
                    metrics["_thinking_budget_retry_count"] = metrics.get("_thinking_budget_retry_count", 0) + 1
                    # Parse the error to extract actual values
                    import re as _re
                    _tb_match = _re.search(r'thinking_budget\s*\[(\d+)\]', err_str)
                    _mc_match = _re.search(r'max_completion_tokens\s*\[(\d+)\]', err_str)
                    if _tb_match and _mc_match:
                        actual_tb = int(_tb_match.group(1))
                        actual_mc = int(_mc_match.group(1))
                        # Fix: set max_completion_tokens = thinking_budget + output margin
                        OUTPUT_TOKEN_MARGIN = 8192
                        fixed_mc = actual_tb + OUTPUT_TOKEN_MARGIN
                        _log("THINKFIX", f"thinking_budget={actual_tb} > max_completion_tokens={actual_mc} → fixing to {fixed_mc}")
                        oai_body_fixed = json.loads(oai_data.decode("utf-8"))
                        oai_body_fixed["max_tokens"] = fixed_mc
                        oai_body_fixed["max_completion_tokens"] = fixed_mc
                        if "thinking_budget" not in oai_body_fixed:
                            oai_body_fixed["thinking_budget"] = actual_tb
                        fixed_data = json.dumps(oai_body_fixed).encode("utf-8")
                        fixed_headers = dict(headers_out)
                        fixed_headers["Content-Length"] = str(len(fixed_data))
                        _log_error_detail({
                            "request_id": request_id,
                            "timestamp": datetime.datetime.now().isoformat(),
                            "error_subcategory": "400_thinking_budget_fix_retry",
                            "upstream_status": resp.status,
                            "original_max_completion_tokens": actual_mc,
                            "original_thinking_budget": actual_tb,
                            "fixed_max_completion_tokens": fixed_mc,
                            "upstream_error_body_full": err_str[:1000],
                        })
                        try:
                            conn_fix = self._make_upstream_conn(parsed_upstream)
                            conn_fix.request("POST", parsed_upstream.path, body=fixed_data, headers=fixed_headers)
                            resp_fix = conn_fix.getresponse()
                            if resp_fix.status < 400:
                                if is_stream:
                                    self._stream_to_anth(resp_fix, request_model, mapped_model, conn_fix, metrics, t_start)
                                    metrics["status"] = 200
                                    metrics["thinking_budget_fix_success"] = True
                                    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                    _log_metrics(metrics)
                                    return
                                elif force_stream_for_nonstream:
                                    self._collect_stream_to_anth(resp_fix, request_model, mapped_model, conn_fix, metrics, t_start)
                                    metrics["thinking_budget_fix_success"] = True
                                    return
                                else:
                                    ttfb_fix = time.time()
                                    resp_body_fix = resp_fix.read()
                                    oai_resp_fix = json.loads(resp_body_fix)
                                    anth_resp_fix = openai_to_anth(oai_resp_fix, request_model)
                                    metrics["status"] = 200
                                    metrics["thinking_budget_fix_success"] = True
                                    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                    metrics["ttfb_ms"] = int((ttfb_fix - t_start) * 1000)
                                    _log_metrics(metrics)
                                    self._send_json(200, anth_resp_fix)
                                    conn_fix.close()
                                    return
                            # Fix retry also failed — fall through to error reporting
                            error_body_fix = resp_fix.read()
                            try:
                                error_json_fix = json.loads(error_body_fix)
                            except Exception:
                                error_json_fix = {"error": error_body_fix.decode("utf-8", errors="replace")}
                            conn_fix.close()
                            error_json = error_json_fix
                            _log("ERR", f"thinking_budget fix retry also failed: {resp_fix.status} {json.dumps(error_json_fix)[:200]}")
                        except Exception as e_fix:
                            _log("ERR", f"thinking_budget fix retry connection error: {e_fix}")
                            # Fall through to error reporting with original error
                if should_resilience_retry:
                    metrics["_resilience_retry_count"] = metrics.get("_resilience_retry_count", 0) + 1
                    _log("RESILIENCE", f"401/403 AuthError → retry #{metrics['_resilience_retry_count']} (KEY5 cooldown should force different deployment)")
                    _log_error_detail({
                        "request_id": request_id,
                        "timestamp": datetime.datetime.now().isoformat(),
                        "error_subcategory": "401_resilience_retry_triggered",
                        "upstream_status": resp.status,
                        "upstream_error_body_full": error_body.decode("utf-8", errors="replace")[:1000],
                    })
                    try:
                        conn2 = self._make_upstream_conn(parsed_upstream)
                        conn2.request("POST", parsed_upstream.path, body=oai_data, headers=headers_out)
                        resp2 = conn2.getresponse()
                        if resp2.status < 400:
                            # Retry succeeded — stream or read the good response
                            if is_stream:
                                self._stream_to_anth(resp2, request_model, mapped_model, conn2, metrics, t_start)
                                metrics["status"] = 200
                                metrics["resilience_retry_success"] = True
                                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                _log_metrics(metrics)
                                return
                            elif force_stream_for_nonstream:
                                self._collect_stream_to_anth(resp2, request_model, mapped_model, conn2, metrics, t_start)
                                metrics["resilience_retry_success"] = True
                                return
                            else:
                                ttfb_start2 = time.time()
                                resp_body2 = resp2.read()
                                oai_response2 = json.loads(resp_body2)
                                anth_response2 = openai_to_anth(oai_response2, request_model)
                                metrics["status"] = 200
                                metrics["resilience_retry_success"] = True
                                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                                metrics["ttfb_ms"] = int((ttfb_start2 - t_start) * 1000)
                                usage2 = oai_response2.get("usage", {})
                                metrics["input_tokens"] = usage2.get("prompt_tokens", 0)
                                metrics["output_tokens"] = usage2.get("completion_tokens", 0)
                                choices2 = oai_response2.get("choices", [])
                                if choices2:
                                    metrics["finish_reason"] = choices2[0].get("finish_reason")
                                _log_metrics(metrics)
                                self._send_json(200, anth_response2)
                                conn2.close()
                                return
                        # Retry also failed — fall through to error reporting
                        error_body2 = resp2.read()
                        try:
                            error_json2 = json.loads(error_body2)
                        except Exception:
                            error_json2 = {"error": error_body2.decode("utf-8", errors="replace")}
                        conn2.close()
                        # Use the retry error (more recent) for reporting
                        error_json = error_json2
                        resp_status_final = resp2.status
                        _log("ERR", f"resilience retry also failed: {resp2.status} {json.dumps(error_json2)[:200]}")
                    except Exception as e2:
                        _log("ERR", f"resilience retry connection error: {e2}")
                        resp_status_final = resp.status
                else:
                    resp_status_final = resp.status

                # No rate_limit_retry or model fallback:
                # Data proves: RATELIMIT retry 8% success (1/13) with 2s waste.
                # FALLBACK (glm5.1→dsv4p) always fails: UnsupportedParamsError on reasoning_effort.
                # CC handles 429 via rate_limit_error type → CC retries with backoff.
                # LiteLLM handles deployment rotation via num_retries=5.
                # NOTE: 429 is NOT converted to 529 — 529 causes CC "Repeated Overloaded" crash.

                _log("ERR", f"upstream {resp_status_final}: {json.dumps(error_json)[:200]}")
                # Log error detail for analysis
                _log_error_detail({
                    "request_id": request_id,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "error_subcategory": f"{resp_status_final}_upstream_error",
                    "upstream_status": resp_status_final,
                    "upstream_error_body_full": json.dumps(error_json)[:3000],
                })
                metrics["status"] = resp_status_final
                metrics["error_type"] = "UpstreamError"
                metrics["error_message"] = json.dumps(error_json)[:200]
                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                _log_metrics(metrics)

# Convert upstream input-token-overflow 400 errors to invalid_request_error (400).
                # When ModelScope returns 400 "Range of input length should be [1, 202745]",
                # the conversation is too long for the backend. Previously we converted to
                # 529 overloaded_error → CC auto-compact → catastrophic context loss.
                # Now: invalid_request_error → CC stops immediately. User sees the error
                # message and can start a new conversation manually. This is better than
                # CC silently destroying context via auto-compact.
                # Guard: thinking_budget errors are handled separately by resilience retry.
                err_lower = json.dumps(error_json).lower()
                is_input_overflow = (
                    resp_status_final == 400
                    and (
                        ("exceeds" in err_lower and ("token" in err_lower or "limit" in err_lower))
                        or ("range of input length" in err_lower)
                        or ("invalidparameter" in err_lower and ("input length" in err_lower or "input token" in err_lower))
                    )
                    and "thinking_budget" not in err_lower
                )
                if is_input_overflow:
                    _log("INPUT-OVERFLOW", f"400 input overflow → invalid_request_error (CC stops, no compact)")
                    err_msg = json.dumps(error_json)[:500]
                    self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                     "message": f"Input tokens exceed ModelScope limit. Please start a new conversation. Detail: {err_msg}"},
                                     "model": request_model})
                    metrics["status"] = 400
                    metrics["error_type"] = "InputExceedsInvalidRequest"
                    return

                client_status = self._get_upstream_status_for_client(resp_status_final)
                error_payload = self._convert_error(error_json, request_model)
                extra_hdrs = None
                # Add retry-after header for 429 rate_limit_error responses so CC
                # knows how long to wait before retrying. Quota exhaustion (all
                # deployments exhausted) needs longer recovery (30s); RPM limits
                # recover faster (5s). Detect by checking the error message content.
                if client_status == 429:
                    err_msg_lower = json.dumps(error_json).lower()
                    is_quota_exhaustion = (
                        "quota" in err_msg_lower
                        or "exhausted" in err_msg_lower
                        or "insufficient" in err_msg_lower
                        or "balance" in err_msg_lower
                        or "limit reached" in err_msg_lower
                    )
                    retry_seconds = 30 if is_quota_exhaustion else 5
                    extra_hdrs = {"retry-after": str(retry_seconds)}
                    _log("RETRY-AFTER", f"429 rate_limit_error → retry-after={retry_seconds}s (quota={is_quota_exhaustion})")
                # Genuine upstream 529 (overloaded) → pass through as api_error (CC retries a few times then stops).
                # Previously forced overloaded_error to trigger CC auto-compact, but auto-compact
                # causes catastrophic context loss. Now: let _convert_error produce api_error →
                # CC retries 2-3 times then stops. User sees error and can start a new conversation.
                elif client_status == 529:
                    extra_hdrs = {"retry-after": "5"}
                    _log("RETRY-AFTER", f"529 overloaded → retry-after=5s (api_error, CC retries then stops)")
                self._send_json(client_status, error_payload, extra_headers=extra_hdrs)
                return

            if is_stream:
                self._stream_to_anth(resp, request_model, mapped_model, conn, metrics, t_start)
            elif force_stream_for_nonstream:
                # DSv4P non-stream → collect streaming response and synthesize non-stream Anthropic response
                self._collect_stream_to_anth(resp, request_model, mapped_model, conn, metrics, t_start)
            else:
                ttfb_start = time.time()
                resp_body = resp.read()
                oai_response = json.loads(resp_body)
                anth_response = openai_to_anth(oai_response, request_model)
                metrics["status"] = 200
                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                metrics["ttfb_ms"] = int((ttfb_start - t_start) * 1000)
                # Extract usage from response
                usage = oai_response.get("usage", {})
                metrics["input_tokens"] = usage.get("prompt_tokens", 0)
                metrics["output_tokens"] = usage.get("completion_tokens", 0)
                # Extract finish_reason
                choices = oai_response.get("choices", [])
                if choices:
                    metrics["finish_reason"] = choices[0].get("finish_reason")
                _log_metrics(metrics)
                self._send_json(200, anth_response)
                conn.close()

        except Exception as e:
            err_str = str(e)

            # No conn_retry: data shows 3% success rate (1/36) with 3s wait.
            # ConnectionRefused errors happen during container restarts when all
            # requests fail regardless. CC's built-in retry handles recovery.
            _log("ERR", f"upstream connection error: {e}")
            metrics["status"] = 502; metrics["error_type"] = "ConnectionError"; metrics["error_message"] = err_str
            metrics["duration_ms"] = int((time.time() - t_start) * 1000)
            _log_error_detail({
                "request_id": request_id,
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": "ConnectionRefusedError",
                "upstream_status": 502,
                "upstream_headers": {},
                "upstream_error_body_full": err_str[:3000],
            })
            _log_metrics(metrics)
            self._send_json(502, {"type": "error", "error": {"type": "api_error",
                             "message": f"Upstream connection failed: {e}"}, "model": request_model})

    # ─── Streaming SSE conversion ───
    def _stream_to_anth(self, resp, request_model, target_model, conn, metrics, t_start):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        message_start_sent = False
        message_delta_sent = False
        ttfb_recorded = False
        buffer = ""
        next_block_idx = 0
        # Track active content blocks by type to emit content_block_stop at transitions
        active_block_type = None  # "thinking", "text", or "tool_use"
        # With stream_options.include_usage=True, litellm sends usage in a SEPARATE chunk
        # (after the finish_reason chunk), so we must collect it independently.
        streaming_input_tokens = 0
        streaming_output_tokens = 0
        # Defer message_delta until stream ends so we can include real token counts
        # from the usage chunk (which arrives AFTER finish_reason in OpenAI streaming format).
        pending_stop_reason = None

        def _emit_message_start(msg_id=None, input_tokens_est=0):
            """Helper to emit message_start event.
            input_tokens_est: estimated input tokens from request content analysis.
            Since OpenAI streaming doesn't provide prompt_tokens until the final chunk,
            we use the proxy's own estimation for input_tokens in message_start.
            Claude Code SDK accumulates message_start.usage + message_delta.usage.
            """
            nonlocal message_start_sent
            self._send_sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": msg_id or f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message", "role": "assistant",
                    "model": request_model, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens_est, "output_tokens": 0,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0},
                },
            })
            message_start_sent = True

        def _emit_graceful_end(stop_reason="end_turn", output_tokens=0, input_tokens_real=0):
            """Close any active blocks, emit message_delta + message_stop.
            Uses streaming_input_tokens/streaming_output_tokens collected from
            the usage chunk (which arrives after finish_reason in OpenAI streaming).
            If those are 0, falls back to provided output_tokens/input_tokens_real.
            """
            nonlocal message_start_sent, message_delta_sent, active_block_type, pending_stop_reason
            if active_block_type is not None:
                self._send_sse("content_block_stop",
                               {"type": "content_block_stop", "index": next_block_idx - 1})
                active_block_type = None
            if not message_start_sent:
                _emit_message_start(input_tokens_est=metrics.get("estimated_input_tokens", 0))
            if not message_delta_sent:
                # Use the best available token counts:
                # 1. streaming_*_tokens from the usage chunk (most accurate)
                # 2. Fall back to parameters (for error cases)
                # 3. Fall back to metrics dict
                real_output = streaming_output_tokens or output_tokens or metrics.get("output_tokens", 0)
                real_input = streaming_input_tokens or input_tokens_real or metrics.get("input_tokens", 0)
                metrics["output_tokens"] = real_output
                metrics["input_tokens"] = real_input
                # Use pending_stop_reason if finish_reason was already received
                final_stop = pending_stop_reason or stop_reason
                usage_delta = {"output_tokens": real_output}
                if real_input > 0:
                    usage_delta["input_tokens"] = real_input
                self._send_sse("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": final_stop, "stop_sequence": None},
                    "usage": usage_delta,
                })
                message_delta_sent = True
            self._send_sse("message_stop", {"type": "message_stop"})
            metrics["status"] = 200
            metrics["duration_ms"] = int((time.time() - t_start) * 1000)
            _log_metrics(metrics)
            try:
                conn.close()
            except Exception:
                pass

        try:
            while True:
                # Read larger chunks for better throughput (was byte-by-byte)
                chunk = resp.read(8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n\n" in buffer:
                    event_str, buffer = buffer.split("\n\n", 1)
                    lines = event_str.split("\n")
                    event_type = None
                    data_str = ""
                    for line in lines:
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()

                    if not data_str or data_str == "[DONE]":
                        # Stream complete — close gracefully
                        _emit_graceful_end()
                        return

                    # LiteLLM sends data without event: line — only skip if explicitly not "chunk"
                    if event_type and event_type != "chunk":
                        continue

                    try:
                        chunk_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        # Log malformed chunks instead of silently skipping
                        _log("WARN", f"malformed SSE chunk: {data_str[:200]}")
                        continue

                    delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                    finish_reason = chunk_data.get("choices", [{}])[0].get("finish_reason")

                    # ── Collect usage from streaming chunks ──
                    # With stream_options.include_usage=True, litellm sends usage data in
                    # a SEPARATE final chunk (after the finish_reason chunk).
                    # We must collect it here regardless of finish_reason.
                    chunk_usage = chunk_data.get("usage", {})
                    if chunk_usage:
                        pt = chunk_usage.get("prompt_tokens", 0)
                        ct = chunk_usage.get("completion_tokens", 0)
                        if pt > 0:
                            streaming_input_tokens = pt
                            metrics["input_tokens"] = pt
                        if ct > 0:
                            streaming_output_tokens = ct
                            metrics["output_tokens"] = ct

                    # Emit message_start on first real content
                    if not message_start_sent:
                        _emit_message_start(chunk_data.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
                                           input_tokens_est=metrics.get("estimated_input_tokens", 0))

                    # Record TTFB on first meaningful delta
                    if not ttfb_recorded and (delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls")):
                        metrics["ttfb_ms"] = int((time.time() - t_start) * 1000)
                        ttfb_recorded = True

                    # ── Reasoning/thinking content ──
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        if active_block_type != "thinking":
                            # Close previous block if any
                            if active_block_type is not None:
                                self._send_sse("content_block_stop",
                                               {"type": "content_block_stop", "index": next_block_idx - 1})
                            self._send_sse("content_block_start", {
                                "type": "content_block_start", "index": next_block_idx,
                                "content_block": {"type": "thinking", "thinking": "",
                                                  "signature": os.environ.get("THINKING_SIGNATURE", "")},
                            })
                            next_block_idx += 1
                            active_block_type = "thinking"
                        self._send_sse("content_block_delta", {
                            "type": "content_block_delta", "index": next_block_idx - 1,
                            "delta": {"type": "thinking_delta", "thinking": reasoning},
                        })

                    # ── Text content ──
                    text_delta = delta.get("content")
                    # Skip empty content strings (model sends content="" alongside reasoning)
                    if text_delta and active_block_type != "text":
                        # Close previous block if any
                        if active_block_type is not None:
                            self._send_sse("content_block_stop",
                                           {"type": "content_block_stop", "index": next_block_idx - 1})
                        self._send_sse("content_block_start", {
                            "type": "content_block_start", "index": next_block_idx,
                            "content_block": {"type": "text", "text": ""},
                        })
                        next_block_idx += 1
                        active_block_type = "text"
                    if text_delta:
                        self._send_sse("content_block_delta", {
                            "type": "content_block_delta", "index": next_block_idx - 1,
                            "delta": {"type": "text_delta", "text": text_delta},
                        })

                    # ── Tool calls ──
                    tool_calls = delta.get("tool_calls", [])
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        if tc.get("id"):
                            # New tool call — close previous block, start new tool_use block
                            if active_block_type is not None:
                                self._send_sse("content_block_stop",
                                               {"type": "content_block_stop", "index": next_block_idx - 1})
                            self._send_sse("content_block_start", {
                                "type": "content_block_start", "index": next_block_idx,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tc["id"],
                                    "name": fn.get("name", ""),
                                    "input": {},
                                },
                            })
                            next_block_idx += 1
                            active_block_type = "tool_use"
                            # The first tool call chunk may include partial arguments
                            # (e.g., "{") — must emit input_json_delta for them
                            if fn.get("arguments"):
                                self._send_sse("content_block_delta", {
                                    "type": "content_block_delta", "index": next_block_idx - 1,
                                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                                })
                        elif fn.get("arguments") and active_block_type == "tool_use":
                            self._send_sse("content_block_delta", {
                                "type": "content_block_delta", "index": next_block_idx - 1,
                                "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                            })

                    # ── Finish ──
                    # With stream_options.include_usage=True, the usage chunk arrives AFTER
                    # the finish_reason chunk in OpenAI streaming format. So we must NOT send
                    # message_delta here — we save stop_reason and let _emit_graceful_end
                    # (called at stream end / [DONE]) send it with real token counts.
                    if finish_reason:
                        # Close the last active content block
                        if active_block_type is not None:
                            self._send_sse("content_block_stop",
                                           {"type": "content_block_stop", "index": next_block_idx - 1})
                            active_block_type = None
                        stop_reason = "end_turn"
                        if finish_reason == "length":
                            stop_reason = "max_tokens"
                        elif finish_reason == "tool_calls":
                            stop_reason = "tool_use"
                        pending_stop_reason = stop_reason
                        metrics["finish_reason"] = finish_reason

        except (http.client.RemoteDisconnected, socket.timeout, ConnectionResetError,
                OSError, http.client.IncompleteRead) as e:
            _log("ERR", f"stream connection error: {e}")
            # Close gracefully so CC receives proper message_stop
            _emit_graceful_end()
            return
        except Exception as e:
            _log("ERR", f"stream unexpected error: {e}")
            _emit_graceful_end()
            return

        # Stream ended without [DONE] — close gracefully with message_delta
        _emit_graceful_end()

    # ─── Collect streaming response → non-stream Anthropic response ───
    def _collect_stream_to_anth(self, resp, request_model, target_model, conn, metrics, t_start):
        """Collect a streaming SSE response from upstream and synthesize a non-stream
        Anthropic-format response. Used for DSv4P non-stream requests because ModelScope
        DSv4P non-stream responses include a 'delta' field that crashes LiteLLM's parser.
        """
        reasoning_text = ""
        content_text = ""
        tool_calls_data = []
        finish_reason = "stop"
        total_input_tokens = 0
        total_output_tokens = 0
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        ttfb_recorded = False
        buffer = ""

        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n\n" in buffer:
                    event_str, buffer = buffer.split("\n\n", 1)
                    lines = event_str.split("\n")
                    event_type = None
                    data_str = ""
                    for line in lines:
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()

                    if not data_str or data_str == "[DONE]":
                        # Stream complete
                        break

                    if event_type and event_type != "chunk":
                        continue

                    try:
                        chunk_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if not ttfb_recorded:
                        metrics["ttfb_ms"] = int((time.time() - t_start) * 1000)
                        ttfb_recorded = True

                    msg_id = chunk_data.get("id", msg_id)
                    delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                    fr = chunk_data.get("choices", [{}])[0].get("finish_reason")

                    # Collect reasoning
                    reasoning = delta.get("reasoning_content", "")
                    if reasoning:
                        reasoning_text += reasoning

                    # Collect text content
                    text = delta.get("content", "")
                    if text:
                        content_text += text

                    # Collect tool calls
                    tool_calls = delta.get("tool_calls", [])
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        if tc.get("id"):
                            # New tool call starts
                            tool_calls_data.append({
                                "id": tc["id"],
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", ""),
                            })
                        elif fn.get("arguments") and tool_calls_data:
                            # Continuation of previous tool call
                            tool_calls_data[-1]["arguments"] += fn["arguments"]

                    # Collect usage
                    chunk_usage = chunk_data.get("usage", {})
                    if chunk_usage:
                        total_input_tokens = chunk_usage.get("prompt_tokens", total_input_tokens)
                        total_output_tokens = chunk_usage.get("completion_tokens", total_output_tokens)

                    if fr:
                        finish_reason = fr

            conn.close()
        except Exception as e:
            _log("ERR", f"collect_stream connection error: {e}")
            try:
                conn.close()
            except Exception:
                pass

        # Synthesize Anthropic non-stream response
        content = []
        if reasoning_text:
            content.append({"type": "thinking", "thinking": reasoning_text,
                            "signature": os.environ.get("THINKING_SIGNATURE", "ErUB3WY0k2GCM2h+4O0S3Y3W3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f3Y3f")})
        if content_text:
            content.append({"type": "text", "text": content_text})
        for tc_data in tool_calls_data:
            try:
                input_data = json.loads(tc_data["arguments"])
            except json.JSONDecodeError:
                input_data = {"raw": tc_data["arguments"]}
            content.append({"type": "tool_use", "id": tc_data["id"],
                            "name": tc_data["name"], "input": input_data})
        if not content:
            content.append({"type": "text", "text": ""})

        stop_reason = "end_turn"
        if finish_reason == "length":
            stop_reason = "max_tokens"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"

        metrics["status"] = 200
        metrics["duration_ms"] = int((time.time() - t_start) * 1000)
        metrics["input_tokens"] = total_input_tokens
        metrics["output_tokens"] = total_output_tokens
        metrics["finish_reason"] = finish_reason
        metrics["force_stream_collect_success"] = True
        _log_metrics(metrics)

        anth_response = {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": request_model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
        self._send_json(200, anth_response)

    # ─── Passthrough for OpenAI format requests ───
    def _passthrough_openai(self):
        body_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(body_len) if body_len > 0 else b""
        body = json.loads(raw) if raw else {}
        mapped_model = MODEL_MAP.get(body.get("model", DEFAULT_MODEL), DEFAULT_MODEL)
        upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
        upstream = MODEL_UPSTREAMS[upstream_key]
        litellm_url = upstream["chat_url"]

        # Replace model name in body with mapped LiteLLM model_name
        body["model"] = mapped_model
        forwarded_body = json.dumps(body).encode("utf-8")

        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_KEY}",
            "Content-Length": str(len(forwarded_body)),
        }
        parsed = urllib.parse.urlparse(litellm_url)
        try:
            conn = self._make_upstream_conn(parsed)
            conn.request("POST", parsed.path, body=forwarded_body, headers=headers_out)
            resp = conn.getresponse()
            resp_body = resp.read()
            self.send_response(resp.status)
            for h in ["Content-Type"]:
                v = resp.getheader(h)
                if v:
                    self.send_header(h, v)
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()
        except Exception as e:
            self._send_json(502, {"error": f"Upstream failed: {e}"})

    # ─── /v1/models proxy ───
    def _proxy_models(self):
        all_models = []
        seen_ids = set()
        for model_key, upstream in MODEL_UPSTREAMS.items():
            models_url = upstream["models_url"]
            parsed = urllib.parse.urlparse(models_url)
            try:
                conn = self._make_upstream_conn(parsed)
                conn.request("GET", parsed.path or "/v1/models", headers={"Authorization": f"Bearer {LITELLM_KEY}"})
                resp = conn.getresponse()
                if resp.status != 200:
                    conn.close()
                    continue
                data = json.loads(resp.read())
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    if model_id not in seen_ids:
                        seen_ids.add(model_id)
                        upstream_key = MODEL_MAP.get(model_id, model_id)
                        # Report ModelScope actual limit as context_length so CC knows
                        # the backend capacity. CC's settings.json contextWindow controls
                        # when CC's built-in compact triggers.
                        context_len = MODEL_MAX_INPUT_TOKENS.get(upstream_key, 131072)
                        all_models.append({
                            "id": model_id,
                            "object": "model",
                            "created": m.get("created", 0),
                            "owned_by": m.get("owned_by", ""),
                            "context_length": context_len,
                        })
                conn.close()
            except Exception as e:
                _log("ERROR", f"models proxy error for {model_key}: {e}")
        self._send_json(200, {"object": "list", "data": all_models})

    # ─── Anthropic-format /v1/models endpoints ───
    def _anthropic_models_list(self):
        """Return Anthropic-format model list with context_window.
        CC uses this context_window to decide when to trigger built-in auto-compact.
        Reporting context_window=MODEL_INPUT_TOKEN_SAFETY (120K) tells CC the
        effective capacity, so CC's auto-compact (settings.json autoCompactWindow=110K)
        triggers before hitting ModelScope's actual 202745 limit.
        """
        all_models = []
        seen_ids = set()
        # Include all model IDs from MODEL_MAP (known Claude names + our names)
        for model_id, mapped in MODEL_MAP.items():
            if mapped not in seen_ids:
                seen_ids.add(mapped)
                safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, 128000)
                all_models.append({
                    "id": model_id,
                    "type": "model",
                    "display_name": mapped,
                    "created_at": "2024-01-01T00:00:00Z",
                    "context_window": safety,
                })
        # Also include the canonical model names (glm5.1, dsv4p)
        for model_key in MODEL_UPSTREAMS:
            if model_key not in seen_ids:
                safety = MODEL_INPUT_TOKEN_SAFETY.get(model_key, 128000)
                all_models.append({
                    "id": model_key,
                    "type": "model",
                    "display_name": model_key,
                    "created_at": "2024-01-01T00:00:00Z",
                    "context_window": safety,
                })
        self._send_json(200, {"data": all_models, "has_more": False})

    def _anthropic_model_detail(self, model_id):
        """Return Anthropic-format model detail for a specific model ID.
        Reports context_window=MODEL_INPUT_TOKEN_SAFETY so CC's built-in
        auto-compact triggers at the right time, before hitting ModelScope limit.
        """
        mapped = MODEL_MAP.get(model_id, DEFAULT_MODEL)
        # Use safety limit as context_window so CC compact triggers before backend overflow
        safety = MODEL_INPUT_TOKEN_SAFETY.get(mapped, 128000)
        self._send_json(200, {
            "id": model_id,
            "type": "model",
            "display_name": mapped,
            "created_at": "2024-01-01T00:00:00Z",
            "context_window": safety,
        })

    # ─── Helpers ───
    def _map_model(self, model_name):
        return MODEL_MAP.get(model_name, DEFAULT_MODEL)

    def _convert_error(self, error_json, request_model):
        """Convert OpenAI error format to Anthropic error format.

        IMPORTANT: CC treats different error types differently:
        - authentication_error → CC hard-stops (fatal, won't retry)
        - invalid_request_error → CC stops (client error, won't retry)
        - rate_limit_error → CC retries with backoff
        - api_error → CC retries (server error, recoverable)

        NO longer using overloaded_error — it triggers CC auto-compact which
        causes catastrophic context loss ("completely forgets everything").
        Input overflow now maps to invalid_request_error → CC stops → user
        starts new conversation manually (better than losing all context).

        Mapping strategy:
        - 429 insufficient_quota → rate_limit_error (NOT api_error)
          Reason: quota exhaustion needs CC to wait for recovery (backoff), not
          fail immediately. rate_limit_error's backoff (5s→10s→20s→40s) gracefully
          handles quota recovery periods without CC freezing/crashing.
        - 429 RPM rate-limit → rate_limit_error (CC retries with backoff)
          These are temporary RPM throttles that recover in seconds — correct.
        - 401/403 auth → api_error (NOT authentication_error, to prevent CC freeze)
        - 400 InvalidParameter from ModelScope → api_error (NOT invalid_request_error)
          Reason: CC sent valid Anthropic params. ModelScope rejects them due to
          its own parameter constraints (e.g. thinking_budget > max_completion_tokens).
          This is a server-side compatibility issue, not a client error. CC should
          retry (preflight fix handles the conversion on next attempt).
        - 400 InvalidParameter "Range of input length" → invalid_request_error
          Reason: Input token overflow. Retrying same content never works. CC
          auto-compact (triggered by overloaded_error) destroys context entirely.
          invalid_request_error → CC stops → user starts new conversation.
        - 400 "inappropriate content" → invalid_request_error (NOT api_error)
          Reason: ModelScope content safety filter rejects input as inappropriate.
          This is NOT recoverable by retrying — the same content will always be
          rejected. CC retries api_error infinitely → freeze. invalid_request_error
          makes CC stop immediately (better than freezing forever).
        - Everything else → api_error (CC retries)
        """
        err = error_json.get("error", error_json)
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        msg_lower = msg.lower()
        err_type = "api_error"

        # 429 insufficient_quota → rate_limit_error (NOT api_error)
        # quota exhaustion needs CC to wait for recovery (backoff), not fail immediately.
        # rate_limit_error's backoff (5s→10s→20s→40s) gracefully handles quota recovery.
        # Check for "insufficient_quota" or "quota" + "exceeded" pattern from ModelScope/Aliyun
        err_code = ""
        if isinstance(err, dict):
            err_code = (err.get("code") or "").lower()
        is_quota_exhausted = (
            "insufficient_quota" in err_code
            or ("quota" in msg_lower and "exceeded" in msg_lower)
            or ("exceeded your current quota" in msg_lower)
        )

        if is_quota_exhausted:
            err_type = "rate_limit_error"  # quota exhausted → CC backoff (wait for recovery)
            _log("QUOTA-MAP", f"insufficient_quota → rate_limit_error (msg: {msg[:100]})")
        elif "rate" in msg_lower or "429" in msg_lower:
            err_type = "rate_limit_error"  # RPM throttle → CC retries with backoff

        # ModelScope content safety filter "inappropriate content" → invalid_request_error
        # NOT api_error! CC retries api_error infinitely → same content always rejected → freeze.
        # invalid_request_error makes CC stop immediately (better than freezing forever).
        elif "inappropriate content" in msg_lower:
            err_type = "invalid_request_error"
            _log("CONTENT-MAP", f"inappropriate content → invalid_request_error (msg: {msg[:100]})")

        # Input token overflow from ModelScope → invalid_request_error (CC stops, no compact)
        # ModelScope format: "Range of input length should be [1, 202745]"
        # Retrying the same oversized content never works. Previously mapped to
        # overloaded_error → CC auto-compact → catastrophic context loss. Now:
        # invalid_request_error → CC stops → user sees error, starts new conversation.
        elif ("range of input length" in msg_lower
              or ("invalidparameter" in msg_lower and ("input length" in msg_lower or "input token" in msg_lower or "exceeds" in msg_lower))):
            err_type = "invalid_request_error"
        # Intentionally NOT mapping other 400 InvalidParameter to invalid_request_error.
        # CC stops on invalid_request_error, but ModelScope InvalidParameter is a
        # server-side constraint mismatch (e.g. thinking_budget vs max_completion_tokens),
        # not a genuine client error. Mapping to api_error lets CC retry, which
        # gives the proxy's preflight fix another chance to adjust parameters.
        return {"type": "error", "error": {"type": err_type, "message": msg}, "model": request_model}

    def _get_upstream_status_for_client(self, upstream_status):
        """Map upstream HTTP status to client-facing status.

        DO NOT convert 429 → 529. 529 causes CC auto-compact → catastrophic context loss.
        CC should see 429 + rate_limit_error → retries with backoff (correct for rate limits).
        Input overflow errors use 400 + invalid_request_error → CC stops (no compact).
        """
        # 429 passes through as-is — _convert_error() maps both types to rate_limit_error
        # RPM 429 → rate_limit_error (CC backoff retry, correct for RPM)
        # insufficient_quota 429 → rate_limit_error (CC backoff, wait for quota recovery)
        return upstream_status

    def _make_upstream_conn(self, parsed_url):
        host = parsed_url.hostname
        port = parsed_url.port or 80
        return http.client.HTTPConnection(host, port, timeout=PROXY_TIMEOUT)

    def _send_json(self, code, data, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_raw(code, body, "application/json", extra_headers)

    def _send_raw(self, code, body_bytes, content_type="application/json", extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_sse(self, event_type, data_dict):
        data_str = json.dumps(data_dict, ensure_ascii=False)
        msg = f"event: {event_type}\ndata: {data_str}\n\n"
        try:
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, fmt, *args):
        pass  # Suppress default logging

class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

def main():
    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    _log("START", f"Proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    _log("START", f"GLM-5.1 gateway: {MODEL_UPSTREAMS['glm5.1']['chat_url']}")
    _log("START", f"DSv4P gateway: {MODEL_UPSTREAMS['dsv4p']['chat_url']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("STOP", "Shutting down")
        server.shutdown()

if __name__ == "__main__":
    main()
