#!/usr/bin/env python3
"""gateway — Anthropic ↔ OpenAI format converter proxy.

Modular structure:
  config.py     — Constants, env vars, MODEL_MAP, MODEL_UPSTREAMS
  logger.py     — _log, _log_metrics, _log_error_detail
  converters.py — anth_to_openai, openai_to_anth, truncation, text estimation
  stream.py     — stream_to_anth, collect_stream_to_anth (SSE conversion)
  error_mapping.py — convert_error, get_upstream_status_for_client, is_input_overflow
  handlers.py   — ProxyHandler (HTTP routing + request processing)
  app.py        — ThreadedHTTPServer + main entry point
"""