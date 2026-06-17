#!/usr/bin/env python3
"""Anthropic ↔ OpenAI format conversion.

This module handles all message format conversions between Anthropic Messages API
format and OpenAI Chat Completions format. It also includes:
- Tool description truncation (ModelScope requires short descriptions)
- Schema description truncation
- Text character estimation for input token safety checks
"""
import json
import uuid
import os

from .config import (
    MAX_TOOL_DESC, MAX_SCHEMA_DESC, CHARS_PER_TOKEN_ESTIMATE,
    OUTPUT_TOKEN_MARGIN, THINKING_SIGNATURE_DEFAULT,
    THINKING_SUPPORT,
)


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
    """Convert Anthropic Messages API format to OpenAI Chat Completions format."""
    model = target_model or body.get("model", "glm5.2")
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

    # Anthropic thinking → GLM-5.2 thinking_budget + reasoning_effort
    # ModelScope GLM-5.2 requires: max_completion_tokens > thinking_budget
    # Claude Code sends thinking.budget_tokens=32768 (default) with max_tokens=8192
    # We must ensure max_completion_tokens > thinking_budget
    # NOTE: DSv4P does NOT support reasoning_effort — only set it for models with thinking support
    # Use THINKING_SUPPORT dict for multi-agent compatibility (not hardcoded "glm5.2")
    if body.get("thinking") and THINKING_SUPPORT.get(target_model, False):
        thinking_cfg = body["thinking"]
        budget = thinking_cfg.get("budget_tokens", 8000)
        # Pass thinking_budget directly for ModelScope GLM-5.2
        oai_body["thinking_budget"] = budget
        # Ensure max_completion_tokens > thinking_budget (ModelScope constraint)
        # Leave room for actual output after thinking: thinking_budget + output margin
        required_min = budget + OUTPUT_TOKEN_MARGIN
        if output_tokens < required_min:
            output_tokens = required_min
            oai_body["max_tokens"] = output_tokens
            oai_body["max_completion_tokens"] = output_tokens
        # Set reasoning_effort for GLM-5.2 only — DSv4P doesn't support it
        if target_model == "glm5.2":
            if budget >= 10000:
                oai_body["reasoning_effort"] = "high"
            elif budget >= 5000:
                oai_body["reasoning_effort"] = "medium"
            else:
                oai_body["reasoning_effort"] = "low"

    return oai_body


# ─── OpenAI → Anthropic Format Conversion ──────────────────────────────────

def openai_to_anth(oai_response, request_model):
    """Convert OpenAI Chat Completions response to Anthropic Messages API format."""
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
                        "signature": os.environ.get("THINKING_SIGNATURE", THINKING_SIGNATURE_DEFAULT)})

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