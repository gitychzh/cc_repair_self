#!/usr/bin/env python3
"""Error format conversion: OpenAI → Anthropic error types.

CC treats different error types differently:
- authentication_error → CC hard-stops (fatal, won't retry)
- invalid_request_error → CC stops (client error, won't retry)
- rate_limit_error → CC retries with backoff
- api_error → CC retries (server error, recoverable)

Mapping strategy documented in detail in _convert_error docstring.
"""
from .logger import _log


def convert_error(error_json, request_model):
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


def get_upstream_status_for_client(upstream_status):
    """Map upstream HTTP status to client-facing status.

    DO NOT convert 429 → 529. 529 causes CC auto-compact → catastrophic context loss.
    CC should see 429 + rate_limit_error → retries with backoff (correct for rate limits).
    Input overflow errors use 400 + invalid_request_error → CC stops (no compact).
    """
    # 429 passes through as-is — convert_error() maps both types to rate_limit_error
    # RPM 429 → rate_limit_error (CC backoff retry, correct for RPM)
    # insufficient_quota 429 → rate_limit_error (CC backoff, wait for quota recovery)
    return upstream_status


def is_input_overflow(error_json, resp_status):
    """Detect if an upstream 400 error is an input token overflow.

    When ModelScope returns 400 "Range of input length should be [1, 202745]",
    the conversation is too long for the backend. Previously we converted to
    529 overloaded_error → CC auto-compact → catastrophic context loss.
    Now: invalid_request_error → CC stops immediately. User sees the error
    message and can start a new conversation manually. This is better than
    CC silently destroying context via auto-compact.
    Guard: thinking_budget errors are handled separately by resilience retry.
    """
    err_lower = json_to_str_lower(error_json)
    return (
        resp_status == 400
        and (
            ("exceeds" in err_lower and ("token" in err_lower or "limit" in err_lower))
            or ("range of input length" in err_lower)
            or ("invalidparameter" in err_lower and ("input length" in err_lower or "input token" in err_lower))
        )
        and "thinking_budget" not in err_lower
    )


def json_to_str_lower(error_json):
    """Convert error JSON to lowercase string for pattern matching."""
    import json
    return json.dumps(error_json).lower()


def is_quota_exhaustion(error_json):
    """Detect if a 429 error is quota exhaustion vs RPM throttle."""
    import json
    err_msg_lower = json.dumps(error_json).lower()
    return (
        "quota" in err_msg_lower
        or "exhausted" in err_msg_lower
        or "insufficient" in err_msg_lower
        or "balance" in err_msg_lower
        or "limit reached" in err_msg_lower
    )