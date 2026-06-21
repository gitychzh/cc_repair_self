#!/usr/bin/env python3
"""Streaming SSE conversion and non-stream collect+synthesize.

Two modes:
1. _stream_to_anth — real-time SSE chunk → Anthropic SSE event (for CC streaming)
2. _collect_stream_to_anth — collect streaming chunks → synthesize non-stream Anthropic response
   (for ModelScope non-stream requests that must be forced to stream due to 'delta' bug)
"""
import json
import os
import uuid
import time
import datetime
import http.client
import socket

from .config import THINKING_SIGNATURE_DEFAULT, UPSTREAM_TIMEOUT
from .logger import _log, _log_metrics, _log_error_detail


def stream_to_anth(handler, resp, request_model, target_model, conn, metrics, t_start):
    """Real-time SSE conversion: OpenAI streaming chunks → Anthropic SSE events.

    handler: ProxyHandler instance (needed for _send_sse, _send_json)
    """
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

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
        handler._send_sse("message_start", {
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
        # R31.8: detect empty stream — no content block was ever opened means the
        # upstream stream produced no content (only finish_reason / [DONE]).
        if next_block_idx == 0:
            _log("WARN", f"empty_stream_response: stream ended with no content "
                         f"(model={metrics.get('litellm_model','?')} output_tokens={streaming_output_tokens})")
            _log_error_detail({
                "request_id": metrics.get("request_id", "?"),
                "timestamp": datetime.datetime.now().isoformat(),
                "error_subcategory": "empty_stream_response",
                "upstream_status": 200,
                "litellm_model": metrics.get("litellm_model", "?"),
                "variant_idx": metrics.get("variant_idx", "?"),
                "key_idx": metrics.get("key_idx", "?"),
                "streaming_output_tokens": streaming_output_tokens,
                "finish_reason": pending_stop_reason,
            })
            metrics["empty_stream_response"] = True
        if active_block_type is not None:
            handler._send_sse("content_block_stop",
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
            handler._send_sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": final_stop, "stop_sequence": None},
                "usage": usage_delta,
            })
            message_delta_sent = True
        handler._send_sse("message_stop", {"type": "message_stop"})
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
                            handler._send_sse("content_block_stop",
                                           {"type": "content_block_stop", "index": next_block_idx - 1})
                        handler._send_sse("content_block_start", {
                            "type": "content_block_start", "index": next_block_idx,
                            "content_block": {"type": "thinking", "thinking": "",
                                              "signature": os.environ.get("THINKING_SIGNATURE", "")},
                        })
                        next_block_idx += 1
                        active_block_type = "thinking"
                    handler._send_sse("content_block_delta", {
                        "type": "content_block_delta", "index": next_block_idx - 1,
                        "delta": {"type": "thinking_delta", "thinking": reasoning},
                    })

                # ── Text content ──
                text_delta = delta.get("content")
                # Skip empty content strings (model sends content="" alongside reasoning)
                if text_delta and active_block_type != "text":
                    # Close previous block if any
                    if active_block_type is not None:
                        handler._send_sse("content_block_stop",
                                           {"type": "content_block_stop", "index": next_block_idx - 1})
                    handler._send_sse("content_block_start", {
                        "type": "content_block_start", "index": next_block_idx,
                        "content_block": {"type": "text", "text": ""},
                    })
                    next_block_idx += 1
                    active_block_type = "text"
                if text_delta:
                    handler._send_sse("content_block_delta", {
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
                            handler._send_sse("content_block_stop",
                                           {"type": "content_block_stop", "index": next_block_idx - 1})
                        handler._send_sse("content_block_start", {
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
                            handler._send_sse("content_block_delta", {
                                "type": "content_block_delta", "index": next_block_idx - 1,
                                "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                            })
                    elif fn.get("arguments") and active_block_type == "tool_use":
                        handler._send_sse("content_block_delta", {
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
                        handler._send_sse("content_block_stop",
                                           {"type": "content_block_stop", "index": next_block_idx - 1})
                        active_block_type = None
                    stop_reason = "end_turn"
                    if finish_reason == "length":
                        stop_reason = "max_tokens"
                    elif finish_reason == "tool_calls":
                        stop_reason = "tool_use"
                    pending_stop_reason = stop_reason
                    metrics["finish_reason"] = finish_reason

    except socket.timeout as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        _log_error_detail({
            "request_id": metrics.get("request_id", "?"),
            "timestamp": datetime.datetime.now().isoformat(),
            "error_subcategory": "stream_socket_timeout",
            "upstream_timeout_setting_ms": UPSTREAM_TIMEOUT * 1000,
            "elapsed_since_request_start_ms": elapsed_ms,
            "timeout_exceeded_by_ms": elapsed_ms - PROXY_TIMEOUT * 1000 if elapsed_ms > PROXY_TIMEOUT * 1000 else 0,
            "litellm_model": metrics.get("litellm_model", "?"),
            "variant_idx": metrics.get("variant_idx", "?"),
            "key_idx": metrics.get("key_idx", "?"),
            "error_message": str(e)[:200],
        })
        _log("TIMEOUT", f"stream socket timeout after {elapsed_ms}ms (UPSTREAM_TIMEOUT={UPSTREAM_TIMEOUT}s): {e}")
        metrics["error_type"] = "StreamSocketTimeout"
        metrics["timeout_exceeded_by_ms"] = elapsed_ms - UPSTREAM_TIMEOUT * 1000 if elapsed_ms > UPSTREAM_TIMEOUT * 1000 else 0
        # Close gracefully so CC receives proper message_stop
        _emit_graceful_end()
        return
    except (http.client.RemoteDisconnected, ConnectionResetError,
            OSError, http.client.IncompleteRead) as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        error_class = type(e).__name__
        _log("ERR", f"stream {error_class} after {elapsed_ms}ms: {e}")
        _log_error_detail({
            "request_id": metrics.get("request_id", "?"),
            "timestamp": datetime.datetime.now().isoformat(),
            "error_subcategory": f"stream_{error_class}",
            "elapsed_since_request_start_ms": elapsed_ms,
            "litellm_model": metrics.get("litellm_model", "?"),
            "variant_idx": metrics.get("variant_idx", "?"),
            "key_idx": metrics.get("key_idx", "?"),
            "error_message": str(e)[:300],
        })
        # Close gracefully so CC receives proper message_stop
        _emit_graceful_end()
        return
    except Exception as e:
        _log("ERR", f"stream unexpected error: {e}")
        _emit_graceful_end()
        return

    # Stream ended without [DONE] — close gracefully with message_delta
    _emit_graceful_end()


def collect_stream_to_anth(handler, resp, request_model, target_model, conn, metrics, t_start):
    """Collect a streaming SSE response from upstream and synthesize a non-stream
    Anthropic-format response. Used for MS non-stream requests because ModelScope
    MS non-stream responses include a 'delta' field that crashes LiteLLM's parser.

    handler: ProxyHandler instance (needed for _send_json)
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
    except socket.timeout as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        _log_error_detail({
            "request_id": metrics.get("request_id", "?"),
            "timestamp": datetime.datetime.now().isoformat(),
            "error_subcategory": "collect_stream_socket_timeout",
            "upstream_timeout_setting_ms": UPSTREAM_TIMEOUT * 1000,
            "elapsed_since_request_start_ms": elapsed_ms,
            "timeout_exceeded_by_ms": elapsed_ms - PROXY_TIMEOUT * 1000 if elapsed_ms > PROXY_TIMEOUT * 1000 else 0,
            "litellm_model": metrics.get("litellm_model", "?"),
            "variant_idx": metrics.get("variant_idx", "?"),
            "key_idx": metrics.get("key_idx", "?"),
            "error_message": str(e)[:200],
        })
        _log("TIMEOUT", f"collect_stream socket timeout after {elapsed_ms}ms (UPSTREAM_TIMEOUT={UPSTREAM_TIMEOUT}s): {e}")
        metrics["error_type"] = "CollectStreamSocketTimeout"
        metrics["timeout_exceeded_by_ms"] = elapsed_ms - UPSTREAM_TIMEOUT * 1000 if elapsed_ms > UPSTREAM_TIMEOUT * 1000 else 0
        try:
            conn.close()
        except Exception:
            pass
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        error_class = type(e).__name__
        _log("ERR", f"collect_stream {error_class} after {elapsed_ms}ms: {e}")
        _log_error_detail({
            "request_id": metrics.get("request_id", "?"),
            "timestamp": datetime.datetime.now().isoformat(),
            "error_subcategory": f"collect_stream_{error_class}",
            "elapsed_since_request_start_ms": elapsed_ms,
            "error_message": str(e)[:300],
        })
        try:
            conn.close()
        except Exception:
            pass

    # Synthesize Anthropic non-stream response
    content = []
    if reasoning_text:
        content.append({"type": "thinking", "thinking": reasoning_text,
                        "signature": os.environ.get("THINKING_SIGNATURE", THINKING_SIGNATURE_DEFAULT)})
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
    handler._send_json(200, anth_response)