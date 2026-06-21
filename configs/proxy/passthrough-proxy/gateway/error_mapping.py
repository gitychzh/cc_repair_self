#!/usr/bin/env python3
"""Error format conversion for both Anthropic and OpenAI formats.

Anthropic error types (for CC):
  - authentication_error → CC hard-stops (fatal, won't retry)
  - invalid_request_error → CC stops (client error, won't retry)
  - rate_limit_error → CC retries with backoff
  - api_error → CC retries (server error, recoverable)

OpenAI error format (for OpenClaw/OpenCode/Hermes):
  - {"error": {"message": "...", "type": "...", "code": "..."}}
  - Types: rate_limit_error, invalid_request_error, server_error, authentication_error
  - Codes: "429", "400", "502", "401"

Mapping strategy documented in detail in convert_error docstring.
"""
import json

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
    elif (("range of input length" in msg_lower
          or ("invalidparameter" in msg_lower and ("input length" in msg_lower or "input token" in msg_lower or "exceeds" in msg_lower)))
          and "thinking_budget" not in msg_lower):
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
    return json.dumps(error_json).lower()


def is_quota_exhaustion(error_json):
    """R31.8/R35.6: Disabled — never classify 429 as quota exhaustion.

    Previously matched keywords like 'quota/exhausted' in the error body. But
    ModelScope's 429 body uses Aliyun's stock 'exceeded your current quota'
    phrase for BOTH token-burst AND rpm-burst throttling (type=throttling_error
    either way), so the keyword test mislabels every burst 429 as 'quota
    exhausted' (325/331 false positives in a day's logs). LiteLLM does not
    forward ModelScope's ratelimit-*-remaining headers on 429, so the proxy
    cannot read remaining=0 either. Actual daily quota is always ample (verified
    against ModelScope backend). Returning False uniformly means all 429s are
    treated as rate_limit bursts → uniform cycling + fallback behavior, and
    logs stop falsely claiming exhaustion.

    R35.6: This was previously only fixed in cc-proxy (40001/40005). The
    passthrough proxy (40003) still used keyword matching, which caused:
    ModelScope RPM burst 429 → keyword match "quota"/"exhausted" → classified
    as quota_exhausted → all_non_quota_429=False → retry-after:180 → OpenClaw
    client sees 180s retry-after → too_long (>60s) → client gives up → STUCK.
    cc-proxy correctly returns False → all_non_quota_429=True → retry-after:5
    → CC waits 5s → retries → succeeds. This asymmetry was the root cause of
    OpenClaw freezing while CC never froze.
    """
    return False


# ─── OpenAI-format error conversion ────────────────────────────────────────

def format_openai_error_all_keys_exhausted(result, mapped_model, request_model):
    """Format all-keys-exhausted error as OpenAI error format.

    For OpenAI agents (OpenClaw/OpenCode/Hermes), errors must be in OpenAI format:
      {"error": {"message": "...", "type": "...", "code": "429"}}

    Mapping (same logic as Anthropic, different format):
      - All 429 → rate_limit_error + code "429" (agent retries with backoff)
      - Has 500/502/timeout → server_error + code "502" (agent retries)
      - Has connection error → server_error + code "502" (agent retries)
      NEVER use code "529" — same disaster as CC overloaded_error for some agents.
    """
    if result.all_429 and not result.all_non_quota_429:
        cycled_keys = ', '.join(['k' + str(a.get('key_idx', a.get('nv_key_idx', 0))+1) for a in result.key_cycle_attempts])
        return {
            "error": {
                "message": f"All {len(result.key_cycle_attempts)} ModelScope API keys have exhausted their "
                           f"token quota for model {mapped_model}. Please wait for quota recovery "
                           f"(typically 15 minutes). Keys cycled: {cycled_keys}",
                "type": "rate_limit_error",
                "code": "429",
            }
        }, 429
    elif result.all_429 and result.all_non_quota_429:
        cycled_keys = ', '.join(['k' + str(a.get('key_idx', a.get('nv_key_idx', 0))+1) for a in result.key_cycle_attempts])
        return {
            "error": {
                "message": f"All {len(result.key_cycle_attempts)} ModelScope API keys returned transient 429 errors "
                           f"for model {mapped_model}. This is a temporary rate limit — not quota exhaustion. "
                           f"Please retry in a few seconds. Keys cycled: {cycled_keys}",
                "type": "rate_limit_error",
                "code": "429",
            }
        }, 429
    else:
        failure_types = [a.get("error_type", "429") for a in result.key_cycle_attempts]
        timeout_keys = [f"k{a.get('key_idx', a.get('nv_key_idx', 0))+1}" for a in result.key_cycle_attempts if a.get("error_type") == "SocketTimeout"]
        connerr_keys = [f"k{a.get('key_idx', a.get('nv_key_idx', 0))+1}" for a in result.key_cycle_attempts if a.get("error_type") in ("ConnectionRefusedError", "ConnectionError")]
        return {
            "error": {
                "message": f"All {len(result.key_cycle_attempts)} key groups failed for model {mapped_model} "
                           f"after {result.elapsed_ms/1000:.1f}s. Failure types: {failure_types}. "
                           f"Timeout keys: {timeout_keys}. Connection error keys: {connerr_keys}. "
                           f"Please retry — upstream may recover.",
                "type": "server_error",
                "code": "502",
            }
        }, 502


def format_openai_error_upstream(error_json, request_model, resp_status):
    """Format a non-cycling upstream error as OpenAI error format.

    Used for errors that don't cycle (400 input overflow, 400 inappropriate content,
    401/403 auth, etc). Same classification logic as convert_error(), but OpenAI format.

    Mapping:
      - 429 quota/rate → rate_limit_error + code "429"
      - 400 input overflow → invalid_request_error + code "400"
      - 400 inappropriate content → invalid_request_error + code "400"
      - 400 InvalidParameter (thinking_budget) → server_error + code "400" (recoverable by param fix)
      - 401/403 auth → authentication_error + code "401" (but NOT fatal like Anthropic)
      - Everything else → server_error
    """
    err = error_json.get("error", error_json)
    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    msg_lower = msg.lower()

    # Quota exhaustion → rate_limit_error
    err_code = ""
    if isinstance(err, dict):
        err_code = (err.get("code") or "").lower()
    is_quota = (
        "insufficient_quota" in err_code
        or ("quota" in msg_lower and "exceeded" in msg_lower)
        or ("exceeded your current quota" in msg_lower)
    )

    if is_quota:
        return {"error": {"message": msg, "type": "rate_limit_error", "code": "429"}}, 429
    elif "rate" in msg_lower or "429" in msg_lower:
        return {"error": {"message": msg, "type": "rate_limit_error", "code": "429"}}, 429

    # Input overflow → invalid_request_error (agent should stop, not retry)
    # R35.6+: Guarded by "thinking_budget" not in msg_lower — thinking_budget errors
    # should be api_error (retryable) not invalid_request_error (stop), consistent with
    # is_input_overflow() and convert_error() logic.
    elif (("range of input length" in msg_lower
          or ("invalidparameter" in msg_lower and ("input length" in msg_lower or "input token" in msg_lower or "exceeds" in msg_lower)))
          and "thinking_budget" not in msg_lower):
        return {"error": {"message": msg, "type": "invalid_request_error", "code": "400"}}, 400

    # Inappropriate content → invalid_request_error (same content always rejected)
    elif "inappropriate content" in msg_lower:
        return {"error": {"message": msg, "type": "invalid_request_error", "code": "400"}}, 400

    # 401/403 auth → authentication_error (NOT fatal — OpenAI agents retry differently)
    elif resp_status in (401, 403):
        return {"error": {"message": msg, "type": "authentication_error", "code": str(resp_status)}}, resp_status

    # 400 InvalidParameter (thinking_budget etc) → server_error (recoverable)
    elif resp_status == 400 and "invalidparameter" in msg_lower:
        return {"error": {"message": msg, "type": "server_error", "code": "400"}}, 400

    # Everything else → server_error
    else:
        return {"error": {"message": msg, "type": "server_error", "code": str(resp_status)}}, resp_status


# ─── Responses API error format (for Codex CLI / _cx) ──────────────────────

def format_responses_error_all_keys_exhausted(result, mapped_model, request_model):
    """Format all-keys-exhausted error as Responses API error format.

    Responses API errors use a flat structure:
      {"error": {"type": "...", "code": "...", "message": "..."}}

    Mapping (same classification logic as OpenAI format, different structure):
      - All 429 → rate_limit_error + code "429"
      - Has 500/502/timeout → server_error + code "502"
      - Has connection error → server_error + code "502"
    """
    if result.all_429 and not result.all_non_quota_429:
        cycled_keys = ', '.join(['k' + str(a.get('key_idx', a.get('nv_key_idx', 0))+1) for a in result.key_cycle_attempts])
        return {
            "error": {
                "type": "rate_limit_error",
                "code": "429",
                "message": f"All {len(result.key_cycle_attempts)} ModelScope API keys have exhausted their "
                           f"token quota for model {mapped_model}. Please wait for quota recovery "
                           f"(typically 15 minutes). Keys cycled: {cycled_keys}",
            }
        }, 429
    elif result.all_429 and result.all_non_quota_429:
        cycled_keys = ', '.join(['k' + str(a.get('key_idx', a.get('nv_key_idx', 0))+1) for a in result.key_cycle_attempts])
        return {
            "error": {
                "type": "rate_limit_error",
                "code": "429",
                "message": f"All {len(result.key_cycle_attempts)} ModelScope API keys returned transient 429 errors "
                           f"for model {mapped_model}. This is a temporary rate limit — not quota exhaustion. "
                           f"Please retry in a few seconds. Keys cycled: {cycled_keys}",
            }
        }, 429
    else:
        failure_types = [a.get("error_type", "429") for a in result.key_cycle_attempts]
        timeout_keys = [f"k{a.get('key_idx', a.get('nv_key_idx', 0))+1}" for a in result.key_cycle_attempts if a.get("error_type") == "SocketTimeout"]
        connerr_keys = [f"k{a.get('key_idx', a.get('nv_key_idx', 0))+1}" for a in result.key_cycle_attempts if a.get("error_type") in ("ConnectionRefusedError", "ConnectionError")]
        return {
            "error": {
                "type": "server_error",
                "code": "502",
                "message": f"All {len(result.key_cycle_attempts)} key groups failed for model {mapped_model} "
                           f"after {result.elapsed_ms/1000:.1f}s. Failure types: {failure_types}. "
                           f"Timeout keys: {timeout_keys}. Connection error keys: {connerr_keys}. "
                           f"Please retry — upstream may recover.",
            }
        }, 502


def format_responses_error_upstream(error_json, request_model, resp_status):
    """Format a non-cycling upstream error as Responses API error format.

    Used for errors that don't cycle (400 input overflow, 400 inappropriate content,
    401/403 auth, etc). Same classification logic as format_openai_error_upstream(),
    but Responses API flat error structure.

    Mapping:
      - 429 quota/rate → rate_limit_error + code "429"
      - 400 input overflow → invalid_request_error + code "400"
      - 400 inappropriate content → invalid_request_error + code "400"
      - 400 InvalidParameter (thinking_budget) → server_error + code "400" (recoverable)
      - 401/403 auth → authentication_error + code "401"
      - Everything else → server_error
    """
    err = error_json.get("error", error_json)
    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    msg_lower = msg.lower()

    # Quota exhaustion → rate_limit_error
    err_code = ""
    if isinstance(err, dict):
        err_code = (err.get("code") or "").lower()
    is_quota = (
        "insufficient_quota" in err_code
        or ("quota" in msg_lower and "exceeded" in msg_lower)
        or ("exceeded your current quota" in msg_lower)
    )

    if is_quota:
        return {"error": {"type": "rate_limit_error", "code": "429", "message": msg}}, 429
    elif "rate" in msg_lower or "429" in msg_lower:
        return {"error": {"type": "rate_limit_error", "code": "429", "message": msg}}, 429

    # Input overflow → invalid_request_error (agent should stop, not retry)
    # R35.6+: Guarded by "thinking_budget" not in msg_lower — thinking_budget errors
    # should be api_error (retryable) not invalid_request_error (stop), consistent with
    # is_input_overflow() and convert_error() logic.
    elif (("range of input length" in msg_lower
          or ("invalidparameter" in msg_lower and ("input length" in msg_lower or "input token" in msg_lower or "exceeds" in msg_lower)))
          and "thinking_budget" not in msg_lower):
        return {"error": {"type": "invalid_request_error", "code": "400", "message": msg}}, 400

    # Inappropriate content → invalid_request_error (same content always rejected)
    elif "inappropriate content" in msg_lower:
        return {"error": {"type": "invalid_request_error", "code": "400", "message": msg}}, 400

    # 401/403 auth → authentication_error
    elif resp_status in (401, 403):
        return {"error": {"type": "authentication_error", "code": str(resp_status), "message": msg}}, resp_status

    # 400 InvalidParameter (thinking_budget etc) → server_error (recoverable)
    elif resp_status == 400 and "invalidparameter" in msg_lower:
        return {"error": {"type": "server_error", "code": "400", "message": msg}}, 400

    # Everything else → server_error
    else:
        return {"error": {"type": "server_error", "code": str(resp_status), "message": msg}}, resp_status