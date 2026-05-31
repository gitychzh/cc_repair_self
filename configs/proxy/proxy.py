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
  CHARS_PER_TOKEN_ESTIMATE — chars per token for input safety (default: 3.5)
  MODEL_INPUT_TOKEN_SAFETY_GLM51 — glm5.1 input token safety limit (default: 190000)
  MODEL_INPUT_TOKEN_SAFETY_DSV4P  — dsv4p input token safety limit (default: 120000)
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
CHARS_PER_TOKEN_ESTIMATE = float(os.environ.get("CHARS_PER_TOKEN_ESTIMATE", "3.5"))

# Per-model upstream routing — chat_url and models_url
MODEL_UPSTREAMS = {
    "glm5.1": {
        "chat_url": os.environ.get("LITELLM_URL_GLM51", "http://glm5.1_uni41001:4000/v1/chat/completions"),
        "models_url": os.environ.get("LITELLM_MODELS_URL_GLM51", "http://glm5.1_uni41001:4000/v1/models"),
    },
    "dsv4p": {
        "chat_url": os.environ.get("LITELLM_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/chat/completions"),
        "models_url": os.environ.get("LITELLM_MODELS_URL_DSV4P", "http://dsv4p_uni42001:4000/v1/models"),
    },
}
DEFAULT_UPSTREAM_MODEL = "glm5.1"

# Model name → LiteLLM model_name mapping
MODEL_MAP = {
    "glm5.1": "glm5.1", "glm-5.1": "glm5.1", "zhipuai/glm-5.1": "glm5.1",
    "dsv4p": "dsv4p", "deepseek-v4-pro": "dsv4p", "deepseek-ai/deepseek-v4-pro": "dsv4p",
    # Claude Code names → glm5.1
    "claude-sonnet-4-20250514": "glm5.1",
    "claude-sonnet-4-6-20250514": "glm5.1",
    "claude-opus-4-20250514": "glm5.1",
    "claude-opus-4-8-20250514": "glm5.1",
    "claude-haiku-4-5-20251001": "glm5.1",
    "claude-3-5-sonnet-20241022": "glm5.1",
    "claude-3-5-haiku-20241022": "glm5.1",
    "claude-3-opus-20240229": "glm5.1",
}

# Input token safety limits (GLM-5.1: 202745 from ModelScope docs; DSv4P: 131072 conservative)
MODEL_MAX_INPUT_TOKENS = {"glm5.1": 202745, "dsv4p": 131072}
MODEL_INPUT_TOKEN_SAFETY = {
    "glm5.1": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_GLM51", "190000")),
    "dsv4p": int(os.environ.get("MODEL_INPUT_TOKEN_SAFETY_DSV4P", "120000")),
}

DEFAULT_MODEL = "glm5.1"

_log_lock = threading.Lock()
_metrics_lock = threading.Lock()
_error_detail_lock = threading.Lock()

_log_lock = threading.Lock()

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
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            tool_results.append(block)
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                if text_parts:
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
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", False),
    }
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

    # Anthropic thinking → OpenAI reasoning_effort
    if body.get("thinking"):
        thinking_cfg = body["thinking"]
        budget = thinking_cfg.get("budget_tokens", 8000)
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
                "model": request_model, "stop_reason": "end_turn",
                "usage": {"input_tokens": 0, "output_tokens": 0}}

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

# ─── SSE Stream Conversion ────────────────────────────────────────────────

def _stream_chunk_to_anth(chunk_data, request_model, first_chunk=True):
    """Convert an OpenAI SSE chunk to Anthropic streaming event(s)."""
    delta = chunk_data.get("choices", [{}])[0].get("delta", {})
    finish_reason = chunk_data.get("choices", [{}])[0].get("finish_reason")
    events = []

    # First chunk: message_start
    if first_chunk:
        events.append(("message_start", {
            "type": "message_start",
            "message": {"id": chunk_data.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
                        "type": "message", "role": "assistant", "model": request_model,
                        "content": [], "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}},
        }))

    # Thinking/reasoning
    reasoning = delta.get("reasoning_content", "")
    if reasoning:
        events.append(("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }))
        events.append(("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": reasoning},
        }))

    # Text
    text_delta = delta.get("content", "")
    if text_delta:
        idx = 1 if reasoning else 0
        if first_chunk and not reasoning:
            events.append(("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }))
        else:
            idx = 1 if any("thinking" in str(e) for e in events) else 0
            if not reasoning and first_chunk:
                idx = 0
        events.append(("content_block_delta", {
            "type": "content_block_delta",
            "index": idx,
            "delta": {"type": "text_delta", "text": text_delta},
        }))

    # Tool calls (streaming)
    tool_calls_delta = delta.get("tool_calls", [])
    for tc_delta in tool_calls_delta:
        idx = tc_delta.get("index", 0) + (1 if reasoning else 0) + (1 if text_delta and not reasoning else 0)
        tc_fn = tc_delta.get("function", {})
        if tc_delta.get("id"):
            # Tool call start
            try:
                partial_input = json.loads(tc_fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                partial_input = {}
            events.append(("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "tool_use", "id": tc_delta["id"],
                                  "name": tc_fn.get("name", ""), "input": partial_input},
            }))
        elif tc_fn.get("arguments"):
            events.append(("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": tc_fn["arguments"]},
            }))

    # Finish
    if finish_reason:
        stop_reason = "end_turn"
        if finish_reason == "length":
            stop_reason = "max_tokens"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"
        events.append(("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": chunk_data.get("usage", {}).get("completion_tokens", 0)},
        }))

    return events

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
        elif parsed.path in ("/v1/models", "/models"):
            self._proxy_models()
        else:
            self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/", "/v1/models", "/models"):
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

        # Convert Anthropic → OpenAI
        oai_body = anth_to_openai(anth_body)
        mapped_model = oai_body.get("model", DEFAULT_MODEL)
        metrics["mapped_model"] = mapped_model
        metrics["num_messages"] = len(oai_body.get("messages", []))
        metrics["num_tools"] = len(oai_body.get("tools", []))
        metrics["total_input_chars"] = len(json.dumps(oai_body))

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

        # ─── Input token pre-check ───
        model_max_tokens = MODEL_MAX_INPUT_TOKENS.get(upstream_key, 131072)
        model_safety = MODEL_INPUT_TOKEN_SAFETY.get(upstream_key, 120000)
        estimated_tokens = int(metrics["total_input_chars"] / CHARS_PER_TOKEN_ESTIMATE)
        metrics["estimated_input_tokens"] = estimated_tokens
        if estimated_tokens > model_safety:
            _log("INPUT-EXCEED", f"estimated_tokens={estimated_tokens} > safety={model_safety}")
            err_msg = (f"Input exceeds model limit (~{estimated_tokens} estimated input tokens, "
                       f"max {model_max_tokens}). Please start a new conversation.")
            metrics["status"] = 400; metrics["error_type"] = "InputTooLong"; metrics["error_message"] = err_msg
            self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": err_msg}})
            _log_metrics(metrics)
            return

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

            if resp.status >= 400:
                error_body = resp.read()
                try:
                    error_json = json.loads(error_body)
                except Exception:
                    error_json = {"error": error_body.decode("utf-8", errors="replace")}
                _log("ERR", f"upstream {resp.status}: {json.dumps(error_json)[:200]}")
                # Log error detail for analysis
                _log_error_detail({
                    "request_id": request_id,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "error_subcategory": f"{resp.status}_upstream_error",
                    "upstream_status": resp.status,
                    "upstream_headers": dict(resp.getheaders()),
                    "upstream_error_body_full": error_body.decode("utf-8", errors="replace")[:3000],
                })
                metrics["status"] = resp.status
                metrics["error_type"] = "UpstreamError"
                metrics["error_message"] = json.dumps(error_json)[:200]
                metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                _log_metrics(metrics)
                self._send_json(resp.status, self._convert_error(error_json, request_model))
                conn.close()
                return

            if is_stream:
                self._stream_to_anth(resp, request_model, mapped_model, conn, metrics, t_start)
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
            _log("ERR", f"upstream connection error: {e}")
            metrics["status"] = 502; metrics["error_type"] = "ConnectionError"; metrics["error_message"] = str(e)
            metrics["duration_ms"] = int((time.time() - t_start) * 1000)
            _log_error_detail({
                "request_id": request_id,
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": "ConnectionRefusedError",
                "upstream_status": 502,
                "upstream_headers": {},
                "upstream_error_body_full": str(e)[:3000],
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

        first_chunk = True
        ttfb_recorded = False
        buffer = ""
        thinking_block_sent = False
        text_block_sent = False

        while True:
            chunk = resp.read(1)
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
                    # Finalize metrics and log
                    metrics["status"] = 200
                    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
                    _log_metrics(metrics)
                    # Send message_stop
                    self._send_sse("message_stop", {"type": "message_stop"})
                    conn.close()
                    return

                if event_type != "chunk":
                    continue

                try:
                    chunk_data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Record TTFT on first meaningful chunk
                if not ttfb_recorded and (delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls")):
                    metrics["ttfb_ms"] = int((time.time() - t_start) * 1000)
                    ttfb_recorded = True

                # Check for finish in stream
                finish_reason = chunk_data.get("choices", [{}])[0].get("finish_reason")
                delta = chunk_data.get("choices", [{}])[0].get("delta", {})

                # Handle reasoning/thinking content
                reasoning = delta.get("reasoning_content", "")
                if reasoning and not thinking_block_sent:
                    self._send_sse("content_block_start", {
                        "type": "content_block_start", "index": 0,
                        "content_block": {"type": "thinking", "thinking": ""},
                    })
                    thinking_block_sent = True
                if reasoning:
                    self._send_sse("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": reasoning},
                    })

                # Handle text content
                text_delta = delta.get("content", "")
                if text_delta and not text_block_sent:
                    idx = 1 if thinking_block_sent else 0
                    if first_chunk and not thinking_block_sent:
                        # Send message_start first
                        self._send_sse("message_start", {
                            "type": "message_start",
                            "message": {"id": chunk_data.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
                                        "type": "message", "role": "assistant",
                                        "model": request_model, "content": [],
                                        "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}},
                        })
                    self._send_sse("content_block_start", {
                        "type": "content_block_start", "index": idx,
                        "content_block": {"type": "text", "text": ""},
                    })
                    text_block_sent = True
                    first_chunk = False

                if text_delta:
                    idx = 1 if thinking_block_sent else 0
                    self._send_sse("content_block_delta", {
                        "type": "content_block_delta", "index": idx,
                        "delta": {"type": "text_delta", "text": text_delta},
                    })
                    if first_chunk:
                        first_chunk = False

                # Handle tool calls in stream
                tool_calls = delta.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    if tc.get("id"):
                        idx = 2 if thinking_block_sent and text_block_sent else (1 if thinking_block_sent or text_block_sent else 0)
                        self._send_sse("content_block_start", {
                            "type": "content_block_start", "index": idx,
                            "content_block": {"type": "tool_use", "id": tc["id"],
                                              "name": fn.get("name", ""), "input": {}},
                        })
                    elif fn.get("arguments"):
                        self._send_sse("content_block_delta", {
                            "type": "content_block_delta",
                            "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                        })

                if finish_reason:
                    stop_reason = "end_turn"
                    if finish_reason == "length":
                        stop_reason = "max_tokens"
                    elif finish_reason == "tool_calls":
                        stop_reason = "tool_use"
                    metrics["finish_reason"] = finish_reason
                    # Extract usage from last chunk
                    chunk_usage = chunk_data.get("usage", {})
                    metrics["output_tokens"] = chunk_usage.get("completion_tokens", 0) or metrics.get("output_tokens", 0)
                    metrics["input_tokens"] = chunk_usage.get("prompt_tokens", 0) or metrics.get("input_tokens", 0)
                    self._send_sse("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                        "usage": {"output_tokens": chunk_usage.get("completion_tokens", 0) or 0},
                    })

        conn.close()

    # ─── Passthrough for OpenAI format requests ───
    def _passthrough_openai(self):
        body_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(body_len) if body_len > 0 else b""
        body = json.loads(raw) if raw else {}
        mapped_model = MODEL_MAP.get(body.get("model", DEFAULT_MODEL), DEFAULT_MODEL)
        upstream_key = mapped_model if mapped_model in MODEL_UPSTREAMS else DEFAULT_UPSTREAM_MODEL
        upstream = MODEL_UPSTREAMS[upstream_key]
        litellm_url = upstream["chat_url"]

        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_KEY}",
        }
        parsed = urllib.parse.urlparse(litellm_url)
        try:
            conn = self._make_upstream_conn(parsed)
            conn.request("POST", parsed.path, body=raw, headers=headers_out)
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

    # ─── Helpers ───
    def _map_model(self, model_name):
        return MODEL_MAP.get(model_name, DEFAULT_MODEL)

    def _convert_error(self, error_json, request_model):
        """Convert OpenAI error format to Anthropic error format."""
        err = error_json.get("error", error_json)
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        err_type = "api_error"
        if "rate" in msg.lower() or "429" in msg:
            err_type = "rate_limit_error"
        elif "invalid" in msg.lower() or "400" in msg:
            err_type = "invalid_request_error"
        return {"type": "error", "error": {"type": err_type, "message": msg}, "model": request_model}

    def _make_upstream_conn(self, parsed_url):
        host = parsed_url.hostname
        port = parsed_url.port or 80
        return http.client.HTTPConnection(host, port, timeout=PROXY_TIMEOUT)

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_raw(code, body, "application/json")

    def _send_raw(self, code, body_bytes, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
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

def main():
    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    _log("START", f"Proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    _log("START", f"GLM-5.1 gateway: {LITELLM_URL_GLM51}")
    _log("START", f"DSv4P gateway: {LITELLM_URL_DSV4P}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("STOP", "Shutting down")
        server.shutdown()

if __name__ == "__main__":
    main()
