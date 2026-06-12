#!/usr/bin/env python3
"""gateway — Anthropic ↔ OpenAI format converter proxy with multi-agent support.

Modular structure (R23→R24):
  config.py       — Constants, env vars, MODEL_MAP, AGENT_SUFFIXES, round-robin
  upstream.py     — Shared v×k cycling executor + UpstreamResult (R23 NEW)
  logger.py       — _log, _log_metrics, _log_error_detail
  converters.py   — anth_to_openai, openai_to_anth, truncation, text estimation
  stream.py       — stream_to_anth, collect_stream_to_anth (SSE conversion)
  error_mapping.py — convert_error (Anthropic), format_openai_error (OpenAI), format_responses_error (_cx), is_input_overflow
  codex.py        — Responses API format conversion + handler for Codex CLI (_cx) (R24 NEW)
  handlers.py     — ProxyHandler (HTTP routing + agent-type dispatch)
  app.py          — ThreadedHTTPServer + main entry point
"""