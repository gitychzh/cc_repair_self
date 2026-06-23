#!/usr/bin/env python3
"""Error format conversion for Hermes NV proxy — OpenAI format only.

Hermes uses OpenAI format (/v1/chat/completions), so errors must be:
  {"error": {"message": "...", "type": "...", "code": "..."}}

Mapping:
  - All NV 429 → rate_limit_error + code "429" (agent retries with backoff)
  - NV timeout/connection error → server_error + code "502" (agent retries)
  - 400 Unsupported parameter → server_error + code "400" (recoverable by strip+retry)
  - 400 input overflow → invalid_request_error + code "400" (agent stops, not retry)
  - 400 inappropriate content → invalid_request_error + code "400" (always rejected)
  - 401/403 auth → authentication_error + code "401"/"403"
  - Everything else → server_error
"""
import json


def format_nv_all_keys_exhausted(result, mapped_model, request_model):
    """Format all NV keys exhausted error as OpenAI error format."""
    nv_cycled = ', '.join(['k' + str(a.get('nv_key_idx', 0)+1) for a in result.key_cycle_attempts])
    if result.all_429:
        return {
            "error": {
                "message": f"All {len(result.key_cycle_attempts)} NV API keys returned 429 errors "
                           f"for model {mapped_model}. Please retry in a few seconds. "
                           f"NV keys cycled: {nv_cycled}",
                "type": "rate_limit_error",
                "code": "429",
            }
        }, 429
    else:
        failure_types = [a.get("error_type", "429") for a in result.key_cycle_attempts]
        timeout_keys = [f"k{a.get('nv_key_idx',0)+1}" for a in result.key_cycle_attempts if a.get("error_type") == "NVSocketTimeout"]
        return {
            "error": {
                "message": f"All {len(result.key_cycle_attempts)} NV API keys failed for model {mapped_model} "
                           f"after {result.elapsed_ms/1000:.1f}s. Failure types: {failure_types}. "
                           f"Timeout keys: {timeout_keys}. Please retry — upstream may recover.",
                "type": "server_error",
                "code": "502",
            }
        }, 502


def format_nv_error_upstream(error_json, request_model, resp_status):
    """Format a non-cycling NV upstream error as OpenAI error format."""
    err = error_json.get("error", error_json)
    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    msg_lower = msg.lower()

    # 429 rate limit
    if "rate" in msg_lower or "429" in msg_lower or resp_status == 429:
        return {"error": {"message": msg, "type": "rate_limit_error", "code": "429"}}, 429

    # 400 Unsupported parameter → server_error (recoverable by strip+retry)
    if resp_status == 400 and "unsupported parameter" in msg_lower:
        return {"error": {"message": msg, "type": "server_error", "code": "400"}}, 400

    # 400 input overflow → invalid_request_error (agent stops)
    if resp_status == 400 and ("exceeds" in msg_lower or "range of input" in msg_lower):
        return {"error": {"message": msg, "type": "invalid_request_error", "code": "400"}}, 400

    # 400 inappropriate content → invalid_request_error (always rejected)
    if resp_status == 400 and "inappropriate content" in msg_lower:
        return {"error": {"message": msg, "type": "invalid_request_error", "code": "400"}}, 400

    # 401/403 auth
    if resp_status in (401, 403):
        return {"error": {"message": msg, "type": "authentication_error", "code": str(resp_status)}}, resp_status

    # Everything else → server_error
    return {"error": {"message": msg, "type": "server_error", "code": str(resp_status)}}, resp_status


def is_quota_exhaustion(error_json):
    """Always False — NV 429 is RPM rate limit, not quota exhaustion."""
    return False
