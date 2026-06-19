#!/usr/bin/env python3
"""gateway — multi-proxy gateway with role-based endpoint serving.

R29: Three proxy containers, each with PROXY_ROLE determining behavior:
  cc          → /v1/messages (Anthropic) → glm5.1 v×k
  codex       → /v1/responses (Responses API) → glm5.1 v×k
  passthrough → /v1/chat/completions (OpenAI passthrough) → dsv4p v×k

Modular structure (R23→R29):
  config.py       — Constants, env vars, MODEL_MAP, AGENT_SUFFIXES, PROXY_ROLE, round-robin
  upstream.py     — Shared v×k cycling executor + UpstreamResult (R29: removed LiteLLM fallback)
  logger.py       — _log, _log_metrics, _log_error_detail
  converters.py   — anth_to_openai, openai_to_anth, truncation, text estimation
  stream.py       — stream_to_anth, collect_stream_to_anth (SSE conversion)
  error_mapping.py — convert_error (Anthropic), format_openai_error (OpenAI), format_responses_error (_cx)
  codex.py        — Responses API format conversion + handler for Codex CLI (_cx)
  handlers.py     — ProxyHandler (role-based HTTP routing + agent-type dispatch)
  app.py          — ThreadedHTTPServer + main entry point
"""
