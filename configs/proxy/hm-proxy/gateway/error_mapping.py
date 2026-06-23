#!/usr/bin/env python3
"""Error format conversion for Hermes NV proxy — OpenAI format — R38.6.

R38.4: _hm_nv dual suffix for model tiers.
Handles multi-tier fallback failures with comprehensive error messages.
"""
import json


def format_nv_all_keys_exhausted(result, mapped_model, request_model):
    """Format all-tiers-exhausted error as OpenAI error format (R38.2).

    Includes detailed info about which tiers were tried and their failure types.
    """
    tiers_tried = result.fallback_tiers_used or []
    tier_summaries = result.tier_attempts or []

    # Build per-tier failure summary
    tier_details = []
    for ts in tier_summaries:
        tier_name = ts.get("tier", "?")
        n = ts.get("num_attempts", 0)
        if ts.get("all_429"):
            tier_details.append(f"{tier_name}: {n}×429")
        elif ts.get("all_empty_200"):
            tier_details.append(f"{tier_name}: {n}×empty200")
        else:
            tier_details.append(f"{tier_name}: {n}×mixed")

    tier_str = ", ".join(tier_details) if tier_details else "unknown"

    # Classify overall error type
    if result.all_429:
        return {
            "error": {
                "message": f"All NV API tiers exhausted for {mapped_model}. "
                           f"Tiers tried: [{tier_str}]. Please retry in a few seconds.",
                "type": "rate_limit_error",
                "code": "429",
            }
        }, 429
    else:
        return {
            "error": {
                "message": f"All NV API tiers failed for {mapped_model} "
                           f"after {result.elapsed_ms/1000:.1f}s. "
                           f"Tiers tried: [{tier_str}]. Please retry — upstream may recover.",
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
