#!/usr/bin/env python3
"""Responses API format conversion and request handling for Codex CLI (_cx).

Converts between OpenAI Responses API format (used by Codex CLI) and
Chat Completions format (used by ModelScope upstream via LiteLLM):

  Request:  Responses API input  → Chat Completions messages body
  Response: Chat Completions response → Responses API output object
  Stream:   Chat Completions SSE chunks → Responses API named SSE events
  Errors:   Upstream errors → Responses API error format

All upstream communication uses Chat Completions format (via upstream.py).
This module is the sole authority on Responses API format specifics.
"""
import json
import uuid
import time
import datetime
import http.client
import socket

from .config import (
    LITELLM_KEY, PROXY_TIMEOUT, MODEL_MAP, DEFAULT_MODEL,
    CHARS_PER_TOKEN_ESTIMATE, NUM_KEYS,
)
from .logger import _log, _log_metrics, _log_error_detail
from .upstream import execute_request, UpstreamResult
from .error_mapping import (
    is_input_overflow, is_quota_exhaustion,
    format_responses_error_all_keys_exhausted,
    format_responses_error_upstream,
)


# ─── Request Conversion: Responses API → Chat Completions ──────────────────

def responses_to_chat_body(cx_body, target_model):
    """Convert Responses API request body → Chat Completions request body.

    Key mappings:
      - cx_body["instructions"] → system message (prepend to messages)
      - cx_body["input"] (string or array) → user/assistant/tool messages
      - cx_body["tools"] → Chat Completions function tools (mostly compatible)
      - cx_body["stream"] → oai_body["stream"]
      - cx_body["temperature"] → oai_body["temperature"]
      - cx_body["max_output_tokens"] → oai_body["max_completion_tokens"]
      - cx_body["response_format"] → oai_body["response_format"]
      - cx_body["previous_response_id"] → not supported (Codex must send full history)

    Args:
        cx_body: dict — Responses API request body
        target_model: str — backend model name ("glm5.1")

    Returns:
        dict — Chat Completions format request body ready for upstream.py
    """
    oai_messages = []

    # 1. Instructions → system message
    instructions = cx_body.get("instructions", "")
    if instructions:
        oai_messages.append({"role": "system", "content": instructions})

    # 2. Input → messages
    #    Responses API input can be:
    #    - string → single user message
    #    - array of content items (messages, function_call_output, etc.)
    input_data = cx_body.get("input", "")
    if isinstance(input_data, str):
        if input_data:
            oai_messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "message":
                    # Response message item → Chat Completions message
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    if isinstance(content, list):
                        # Content array — extract text parts
                        text_parts = []
                        for c in content:
                            if isinstance(c, dict):
                                if c.get("type") == "input_text":
                                    text_parts.append(c.get("text", ""))
                                elif c.get("type") == "input_image":
                                    # Image reference — convert to image_url
                                    img_url = c.get("image_url", "")
                                    oai_messages.append({
                                        "role": role,
                                        "content": [{
                                            "type": "image_url",
                                            "image_url": {"url": img_url}
                                        }]
                                    })
                                else:
                                    text_parts.append(c.get("text", str(c)))
                            elif isinstance(c, str):
                                text_parts.append(c)
                        if text_parts:
                            oai_messages.append({"role": role, "content": "\n".join(text_parts)})
                    elif isinstance(content, str):
                        oai_messages.append({"role": role, "content": content})
                    else:
                        oai_messages.append({"role": role, "content": str(content)})

                elif item_type == "function_call":
                    # Previous function call from model → assistant message with tool_calls
                    oai_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": item.get("call_id", f"call_{uuid.uuid4().hex[:24]}"),
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }],
                    })

                elif item_type == "function_call_output":
                    # Tool result → tool message
                    oai_messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "content": item.get("output", ""),
                    })

                else:
                    # Unknown item type — try to extract text
                    content = item.get("content", item.get("text", ""))
                    if content:
                        oai_messages.append({"role": "user", "content": str(content)})

            elif isinstance(item, str):
                oai_messages.append({"role": "user", "content": item})

    if not oai_messages:
        oai_messages.append({"role": "user", "content": "test"})

    # 3. Build Chat Completions body
    oai_body = {
        "model": target_model,
        "messages": oai_messages,
        "stream": cx_body.get("stream", False),
    }

    # 4. Output tokens limit
    max_output = cx_body.get("max_output_tokens")
    if max_output:
        oai_body["max_tokens"] = max_output
        oai_body["max_completion_tokens"] = max_output
    else:
        oai_body["max_tokens"] = 4096
        oai_body["max_completion_tokens"] = 4096

    # 5. Temperature
    if cx_body.get("temperature"):
        oai_body["temperature"] = cx_body["temperature"]

    # 6. Tools — Responses API function tools → Chat Completions function tools
    # Only convert tools that have a valid function.name — ModelScope requires name
    # and rejects empty strings. Skip all non-function type tools (Codex built-ins
    # like computer_call_preview, web_search_preview, etc. — not supported by ModelScope).
    cx_tools = cx_body.get("tools", [])
    if cx_tools:
        oai_tools = []
        skipped_tools = []
        for tool in cx_tools:
            tool_type = tool.get("type", "")
            if tool_type == "function":
                fn = tool.get("function", {})
                fn_name = fn.get("name", "")
                if not fn_name:
                    # Skip function tools without name — would cause LiteLLM 400 error
                    skipped_tools.append(f"function(no-name)")
                    continue
                oai_tools.append({
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    },
                })
            else:
                # Skip non-function tools (web_search, file_search, code_interpreter,
                # computer_call_preview, etc.) — ModelScope doesn't support them
                skipped_tools.append(f"{tool_type}({tool.get('name', tool.get('id', '?'))})")
        if oai_tools:
            oai_body["tools"] = oai_tools
        if skipped_tools:
            _log("CX-TOOLS-SKIP", f"skipped {len(skipped_tools)}/{len(cx_tools)} tools not supported by ModelScope: {skipped_tools[:5]}")
            if not oai_tools:
                # All tools were skipped — don't send empty tools array
                _log("CX-TOOLS-ALL-SKIP", f"all {len(cx_tools)} tools skipped — no tools in Chat Completions request")

    # 7. Tool choice — only include if we have valid tools (otherwise "required"/dict
    # would fail because ModelScope expects matching tool names)
    tool_choice = cx_body.get("tool_choice")
    if tool_choice and oai_tools:
        if isinstance(tool_choice, str):
            oai_body["tool_choice"] = tool_choice  # "auto", "required", "none"
        elif isinstance(tool_choice, dict):
            oai_body["tool_choice"] = tool_choice
    elif tool_choice and not oai_tools:
        _log("CX-TOOL-CHOICE-SKIP", f"tool_choice={tool_choice} skipped because no valid tools converted")

    # 8. Response format
    response_format = cx_body.get("response_format")
    if response_format:
        oai_body["response_format"] = response_format

    # 9. Request usage in streaming
    if oai_body.get("stream") and "stream_options" not in oai_body:
        oai_body["stream_options"] = {"include_usage": True}

    return oai_body


# ─── Response Conversion: Chat Completions → Responses API ──────────────────

def _gen_resp_id():
    """Generate a response ID in Responses API format: resp_xxxxxxxx."""
    return f"resp_{uuid.uuid4().hex[:24]}"

def _gen_msg_id():
    """Generate a message output item ID."""
    return f"msg_{uuid.uuid4().hex[:24]}"

def _gen_fc_id():
    """Generate a function_call output item ID."""
    return f"fc_{uuid.uuid4().hex[:24]}"

def _gen_call_id():
    """Generate a call_id for function calls."""
    return f"call_{uuid.uuid4().hex[:24]}"


def chat_to_responses(oai_response, request_model):
    """Convert Chat Completions response → Responses API response object.

    Chat Completions format:
      {"choices": [{"message": {"content": "...", "tool_calls": [...]}}],
       "usage": {"prompt_tokens": ..., "completion_tokens": ...}}

    Responses API format:
      {"id": "resp_...", "object": "response", "status": "completed",
       "output": [{"type": "message", ...}, {"type": "function_call", ...}],
       "usage": {"input_tokens": ..., "output_tokens": ...}}

    Args:
        oai_response: dict — Chat Completions response from upstream
        request_model: str — frontend model name (e.g. "glm5.1_cx")

    Returns:
        dict — Responses API format response object
    """
    resp_id = _gen_resp_id()
    output = []
    usage = oai_response.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    # Extract the first choice
    choices = oai_response.get("choices", [])
    if not choices:
        output.append({
            "type": "message",
            "id": _gen_msg_id(),
            "role": "assistant",
            "content": [{"type": "output_text", "text": ""}],
            "status": "completed",
        })
    else:
        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        # 1. Message output item (text content)
        msg_id = _gen_msg_id()
        msg_content = []

        # Reasoning/thinking content — not in Responses API spec,
        # but we include it as a text annotation for transparency
        reasoning = message.get("reasoning_content", "")
        text_content = message.get("content", "")

        if text_content:
            msg_content.append({"type": "output_text", "text": text_content})
        if not msg_content:
            msg_content.append({"type": "output_text", "text": ""})

        msg_status = "completed"
        if finish_reason == "length":
            msg_status = "incomplete"

        output.append({
            "type": "message",
            "id": msg_id,
            "role": "assistant",
            "content": msg_content,
            "status": msg_status,
        })

        # 2. Function call output items (separate from message)
        tool_calls = message.get("tool_calls", [])
        for tc in tool_calls:
            fn = tc.get("function", {})
            output.append({
                "type": "function_call",
                "id": _gen_fc_id(),
                "call_id": tc.get("id", _gen_call_id()),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", "{}"),
                "status": "completed",
            })

    # Build response object
    response_obj = {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": request_model,
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "metadata": {},
    }

    # Include reasoning as metadata if present (for debugging)
    if reasoning:
        response_obj["metadata"]["reasoning_content"] = reasoning[:500]

    return response_obj


# ─── Streaming Conversion: Chat Completions SSE → Responses API SSE ────────

def stream_responses_passthrough(handler, resp, conn, metrics, t_start, request_model, request_id):
    """Convert Chat Completions streaming SSE → Responses API named SSE events.

    Responses API SSE uses named events (event: response.output_text.delta)
    instead of generic data chunks.

    Event sequence:
      1. response.created          — initial response object
      2. response.in_progress      — response status → in_progress
      3. response.output_item.added  — new output item (message or function_call)
      4. response.content_part.added — content part in message item
      5. response.output_text.delta  — incremental text
      6. response.output_text.done   — text complete
      7. response.function_call_arguments.delta — incremental args
      8. response.function_call_arguments.done  — args complete
      9. response.output_item.done   — output item complete
      10. response.completed         — final response with usage

    Args:
        handler: ProxyHandler instance (for _send_sse)
        resp: upstream HTTPResponse (streaming)
        conn: upstream HTTPConnection
        metrics: dict — metrics dict to update
        t_start: float — request start timestamp
        request_model: str — frontend model name
        request_id: str — request ID
    """
    resp_id = _gen_resp_id()
    msg_id = _gen_msg_id()
    output_index = 0  # Current output item index (starts at 0 for message)
    content_index = 0  # Current content part index within message

    ttfb_recorded = False
    streaming_input_tokens = 0
    streaming_output_tokens = 0
    finish_reason = None
    active_tool_calls = {}  # call_id → {name, arguments_buffer}
    text_buffer = ""
    buffer = ""  # SSE chunk buffer

    # ─── Emit initial events ───
    # response.created
    handler._send_sse("response.created", {
        "type": "response.created",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": request_model,
            "status": "in_progress",
            "output": [],
            "metadata": {},
        },
    })

    # response.in_progress
    handler._send_sse("response.in_progress", {
        "type": "response.in_progress",
        "response": {
            "id": resp_id,
            "object": "response",
            "status": "in_progress",
        },
    })

    # response.output_item.added — first message item
    handler._send_sse("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "type": "message",
            "id": msg_id,
            "role": "assistant",
            "content": [],
            "status": "in_progress",
        },
    })

    # response.content_part.added — first text content part
    handler._send_sse("response.content_part.added", {
        "type": "response.content_part.added",
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })

    # Start streaming headers
    # Note: headers already sent by handler before calling this function

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

                # Skip non-chunk events
                if event_type and event_type != "chunk":
                    continue

                try:
                    chunk_data = json.loads(data_str)
                except json.JSONDecodeError:
                    _log("WARN", f"codex stream malformed SSE: {data_str[:200]}")
                    continue

                # Record TTFB
                if not ttfb_recorded:
                    metrics["ttfb_ms"] = int((time.time() - t_start) * 1000)
                    ttfb_recorded = True

                # Collect usage from streaming chunks
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

                delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                fr = chunk_data.get("choices", [{}])[0].get("finish_reason")

                # ── Content delta → response.output_text.delta ──
                # GLM-5.1 sends reasoning_content and content in the SAME delta chunk.
                # For Codex CLI (Responses API), there's no separate "reasoning" output type —
                # we merge both reasoning_content AND content into output_text so Codex gets
                # the full model output. This is different from CC's Anthropic path which
                # splits reasoning into thinking blocks.
                text_delta = delta.get("content") or ""
                reasoning_delta = delta.get("reasoning_content") or ""
                # Merge both into one delta for Codex output_text
                merged_delta = reasoning_delta + text_delta
                if merged_delta:
                    text_buffer += merged_delta
                    handler._send_sse("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "output_index": output_index,
                        "content_index": content_index,
                        "delta": merged_delta,
                    })

                # ── Tool calls → function_call output items ──
                tool_calls = delta.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    tc_id = tc.get("id")

                    if tc_id:
                        # New tool call starts — emit as new output item
                        call_id = tc_id

                        # Close the current message content_part.done
                        handler._send_sse("response.content_part.done", {
                            "type": "response.content_part.done",
                            "output_index": 0,
                            "content_index": content_index,
                            "part": {
                                "type": "output_text",
                                "text": text_buffer,
                                "annotations": [],
                            },
                        })

                        # Close the message output_item.done
                        handler._send_sse("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "type": "message",
                                "id": msg_id,
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": text_buffer, "annotations": []}],
                                "status": "completed",
                            },
                        })

                        # New function_call output item
                        output_index += 1
                        handler._send_sse("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": output_index,
                            "item": {
                                "type": "function_call",
                                "id": f"fc_{uuid.uuid4().hex[:8]}",
                                "call_id": call_id,
                                "name": fn.get("name", ""),
                                "arguments": "",
                                "status": "in_progress",
                            },
                        })

                        active_tool_calls[call_id] = {
                            "name": fn.get("name", ""),
                            "output_index": output_index,
                            "arguments_buffer": fn.get("arguments", ""),
                        }

                        # Emit any initial arguments
                        if fn.get("arguments"):
                            handler._send_sse("response.function_call_arguments.delta", {
                                "type": "response.function_call_arguments.delta",
                                "output_index": output_index,
                                "call_id": call_id,
                                "delta": fn["arguments"],
                            })

                    elif fn.get("arguments") and active_tool_calls:
                        # Continuation of existing tool call — find the last one
                        last_call_id = list(active_tool_calls.keys())[-1]
                        tc_info = active_tool_calls[last_call_id]
                        tc_info["arguments_buffer"] += fn["arguments"]
                        handler._send_sse("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "output_index": tc_info["output_index"],
                            "call_id": last_call_id,
                            "delta": fn["arguments"],
                        })

                # ── Finish reason ──
                if fr:
                    finish_reason = fr
                    metrics["finish_reason"] = fr

    except socket.timeout as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        _log("TIMEOUT", f"codex stream socket timeout after {elapsed_ms}ms: {e}")
        _log_error_detail({
            "request_id": request_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "error_subcategory": "codex_stream_socket_timeout",
            "elapsed_since_request_start_ms": elapsed_ms,
            "error_message": str(e)[:200],
        })
    except (http.client.RemoteDisconnected, ConnectionResetError,
            OSError, http.client.IncompleteRead) as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        error_class = type(e).__name__
        _log("ERR", f"codex stream {error_class} after {elapsed_ms}ms: {e}")
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        error_class = type(e).__name__
        _log("ERR", f"codex stream unexpected {error_class} after {elapsed_ms}ms: {e}")

    # ─── Emit final events (close all open items) ───
    # Close active tool calls
    for call_id, tc_info in active_tool_calls.items():
        handler._send_sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "output_index": tc_info["output_index"],
            "call_id": call_id,
            "arguments": tc_info["arguments_buffer"],
        })
        handler._send_sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": tc_info["output_index"],
            "item": {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:8]}",
                "call_id": call_id,
                "name": tc_info["name"],
                "arguments": tc_info["arguments_buffer"],
                "status": "completed",
            },
        })

    # If no tool calls were emitted, close the message items
    if not active_tool_calls:
        # content_part.done
        handler._send_sse("response.content_part.done", {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": text_buffer,
                "annotations": [],
            },
        })
        # output_item.done (message)
        handler._send_sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": msg_id,
                "role": "assistant",
                "content": [{"type": "output_text", "text": text_buffer, "annotations": []}],
                "status": "completed",
            },
        })

    # response.completed
    input_tok = streaming_input_tokens or metrics.get("input_tokens", 0)
    output_tok = streaming_output_tokens or metrics.get("output_tokens", 0)

    response_status = "completed"
    if finish_reason == "length":
        response_status = "incomplete"

    handler._send_sse("response.completed", {
        "type": "response.completed",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": request_model,
            "status": response_status,
            "output": [],  # Items were sent incrementally
            "usage": {
                "input_tokens": input_tok,
                "output_tokens": output_tok,
                "total_tokens": input_tok + output_tok,
            },
            "metadata": {},
        },
    })

    # Log final metrics
    metrics["status"] = 200
    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
    if streaming_input_tokens > 0:
        metrics["input_tokens"] = streaming_input_tokens
    if streaming_output_tokens > 0:
        metrics["output_tokens"] = streaming_output_tokens
    _log_metrics(metrics)

    try:
        conn.close()
    except Exception:
        pass


# ─── Non-stream response handler ───────────────────────────────────────────

def handle_codex_responses(handler, cx_body, mapped_model, request_model, request_id, metrics, t_start):
    """Handle a Responses API request end-to-end.

    Flow:
      1. Convert Responses API request → Chat Completions request
      2. Execute upstream with v×k cycling (via upstream.execute_request)
      3. Handle errors (all_keys_exhausted or non-cycling)
      4. On success: convert Chat Completions response → Responses API response
         - Non-stream: convert response object
         - Stream: convert SSE events
    """
    # Determine stream preference
    is_stream = cx_body.get("stream", False)
    metrics["_original_stream"] = is_stream
    metrics["stream"] = is_stream

    # Convert Responses API request → Chat Completions format
    oai_body = responses_to_chat_body(cx_body, mapped_model)

    # Force-stream for non-stream requests (ModelScope delta bug workaround)
    # Same as CC path: ModelScope non-stream responses intermittently include
    # a 'delta' field that breaks parsing. Force stream + collect + synthesize.
    force_stream_for_nonstream = not is_stream
    if force_stream_for_nonstream:
        oai_body["stream"] = True
        if "stream_options" not in oai_body:
            oai_body["stream_options"] = {"include_usage": True}  # Needed for usage data in collect mode
        _log("FORCE-STREAM", f"codex non-stream → forcing stream=True (collect+convert)")

    # Log request
    json_chars = len(json.dumps(cx_body))
    metrics["total_input_chars"] = json_chars
    estimated_tokens = int(json_chars / CHARS_PER_TOKEN_ESTIMATE)
    metrics["estimated_input_tokens"] = estimated_tokens
    _log("REQ", f"model={request_model}→{mapped_model} stream={is_stream} "
                f"agent=_cx input_type={type(cx_body.get('input','')).__name__} "
                f"msgs_in_oai={len(oai_body.get('messages',[]))}")

    # Execute upstream request with v×k cycling
    result = execute_request(handler, oai_body, mapped_model, request_id, metrics, t_start)

    if not result.success:
        # ─── Error handling for Responses API format ───
        if result.all_keys_exhausted:
            error_payload, client_status = format_responses_error_all_keys_exhausted(result, mapped_model, request_model)
            extra_hdrs = None
            if client_status == 429:
                extra_hdrs = {"retry-after": "180"}
            handler._send_json(client_status, error_payload, extra_headers=extra_hdrs)
            return
        else:
            # Non-cycling upstream error — format Responses API error
            error_json = result.final_error_json
            resp_status = result.final_resp_status
            error_payload, client_status = format_responses_error_upstream(error_json, request_model, resp_status)
            extra_hdrs = None
            if client_status == 429:
                quota_exhaust = is_quota_exhaustion(error_json)
                retry_seconds = 30 if quota_exhaust else 5
                extra_hdrs = {"retry-after": str(retry_seconds)}
            handler._send_json(client_status, error_payload, extra_headers=extra_hdrs)
            return

    # ─── Success: process response ───
    resp = result.resp
    conn = result.conn
    # R28: Merge upstream result info into handler metrics (key cycling, variant, model details)
    metrics["key_idx"] = result.key_idx
    metrics["variant_idx"] = result.variant_idx
    metrics["litellm_model"] = result.litellm_model
    if result.key_cycle_attempts:
        metrics["key_cycle_429s_before_success"] = len(result.key_cycle_attempts)
        metrics["key_cycle_details"] = result.key_cycle_attempts

    if is_stream:
        # Streaming: send headers then convert SSE events
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        stream_responses_passthrough(handler, resp, conn, metrics, t_start, request_model, request_id)
    elif force_stream_for_nonstream:
        # Collect stream + synthesize Responses API response
        _collect_stream_to_responses(handler, resp, conn, request_model, mapped_model, metrics, t_start)
    else:
        # Direct non-stream response
        ttfb_start = time.time()
        resp_body = resp.read()
        oai_response = json.loads(resp_body)
        cx_response = chat_to_responses(oai_response, request_model)

        metrics["status"] = 200
        metrics["duration_ms"] = int((time.time() - t_start) * 1000)
        metrics["ttfb_ms"] = int((ttfb_start - t_start) * 1000)
        usage = oai_response.get("usage", {})
        metrics["input_tokens"] = usage.get("prompt_tokens", 0)
        metrics["output_tokens"] = usage.get("completion_tokens", 0)
        _log_metrics(metrics)
        handler._send_json(200, cx_response)
        conn.close()


def _collect_stream_to_responses(handler, resp, conn, request_model, mapped_model, metrics, t_start):
    """Collect a forced-stream response and synthesize Responses API non-stream response.

    Same pattern as collect_stream_to_anth in stream.py, but outputs Responses API format.
    """
    reasoning_text = ""
    content_text = ""
    tool_calls_data = []
    finish_reason = "stop"
    total_input_tokens = 0
    total_output_tokens = 0
    buffer = ""
    ttfb_recorded = False

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

                delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                fr = chunk_data.get("choices", [{}])[0].get("finish_reason")

                # Collect reasoning + content — merge into output_text for Codex
                # Same logic as streaming: GLM-5.1 puts reasoning and content in the same
                # delta chunk. For Responses API, there's no separate reasoning output type.
                # Codex needs the full model output (reasoning + content) as output_text.
                reasoning = delta.get("reasoning_content", "")
                if reasoning:
                    reasoning_text += reasoning
                    content_text += reasoning  # Merge reasoning into output_text for Codex

                text = delta.get("content", "")
                if text:
                    content_text += text

                # Collect tool calls
                tool_calls = delta.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    if tc.get("id"):
                        tool_calls_data.append({
                            "id": tc["id"],
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", ""),
                        })
                    elif fn.get("arguments") and tool_calls_data:
                        tool_calls_data[-1]["arguments"] += fn["arguments"]

                # Collect usage
                chunk_usage = chunk_data.get("usage", {})
                if chunk_usage:
                    pt = chunk_usage.get("prompt_tokens", 0)
                    ct = chunk_usage.get("completion_tokens", 0)
                    if pt > 0:
                        total_input_tokens = pt
                    if ct > 0:
                        total_output_tokens = ct

                if fr:
                    finish_reason = fr

        conn.close()
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        error_class = type(e).__name__
        _log("ERR", f"codex collect_stream {error_class} after {elapsed_ms}ms: {e}")
        try:
            conn.close()
        except Exception:
            pass

    # Synthesize Responses API response
    output = []
    msg_id = _gen_msg_id()

    # Message output item
    msg_content = []
    if content_text:
        msg_content.append({"type": "output_text", "text": content_text})
    if not msg_content:
        msg_content.append({"type": "output_text", "text": ""})

    msg_status = "completed"
    if finish_reason == "length":
        msg_status = "incomplete"

    output.append({
        "type": "message",
        "id": msg_id,
        "role": "assistant",
        "content": msg_content,
        "status": msg_status,
    })

    # Function call output items
    for tc_data in tool_calls_data:
        output.append({
            "type": "function_call",
            "id": _gen_fc_id(),
            "call_id": tc_data["id"],
            "name": tc_data["name"],
            "arguments": tc_data["arguments"],
            "status": "completed",
        })

    cx_response = {
        "id": _gen_resp_id(),
        "object": "response",
        "created_at": int(time.time()),
        "model": request_model,
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
        },
        "metadata": {},
    }

    # Include reasoning as metadata
    if reasoning_text:
        cx_response["metadata"]["reasoning_content"] = reasoning_text[:500]

    metrics["status"] = 200
    metrics["duration_ms"] = int((time.time() - t_start) * 1000)
    metrics["input_tokens"] = total_input_tokens
    metrics["output_tokens"] = total_output_tokens
    metrics["finish_reason"] = finish_reason
    metrics["force_stream_collect_success"] = True
    _log_metrics(metrics)

    handler._send_json(200, cx_response)
