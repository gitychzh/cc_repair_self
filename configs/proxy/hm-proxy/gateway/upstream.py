#!/usr/bin/env python3
"""Upstream request executor for Hermes NV proxy — R38.6.

R38.2: Three-tier fallback routing with per-tier persistent RR counters.
R38.3→R38.4: Dual suffix convention: _hm_nv = Hermes+NV, _hm_ms = Hermes+MS.
       Added sock.settimeout() after conn.request() for read timeout
       (R36.2 lesson: HTTPConnection.timeout only controls connect, not read).
R38.5: throttle_outbound() only on first key attempt (not during cycling).
       NV RPM is per-key independent — cycling to a different key has its own
       RPM bucket, so throttle delay within cycling is pure waste.
       429 cooldown restored (lost during R38.4 naming refactor).
R38.6 CRITICAL FIXES (data-driven):
       1. sock.settimeout() moved BEFORE getresponse() (was AFTER → infinite
          read timeout bug; getresponse() blocked 650-770s without timeout).
          Evidence: Round 2 logs showed per-key timeouts of 650-770s far exceeding
          UPSTREAM_TIMEOUT=45s. Root cause: settimeout was set AFTER getresponse()
          returned, so getresponse() used HTTPConnection.timeout (only connect-side).
       2. deepseek_hm_nv removed from fallback chain (5/5 LiteLLM containers →
          all 30s timeout; NV API deepseek-v4-pro completely unreachable).
          Removing saves 225s per request (5 keys × 45s dead time).
       3. Tier timeout budget (TIER_TIMEOUT_BUDGET_S=90s): caps cumulative time
          per tier. Without this, 7 attempts × 45s = 315s worst case per tier.
       4. Attempt range: HM_NUM_KEYS*2=10 → HM_NUM_KEYS+2=7 (cooldown skips
          still covered, but worst case reduced from 450s → 315s → 90s with budget).
       5. KEY_COOLDOWN_S: 30→15s (NV per-key 429 recovery ~15s, data: burst throttle).
       6. MIN_OUTBOUND_INTERVAL_S: 3.5→1.5s (5 NV keys independent RPM, throttle
          only needs to prevent burst, not 3.5s per-request delay).
       7. Connection:close header (prevent connection reuse → BrokenPipe errors).

Default tier: glm5.1_hm_nv (5 keys, sequential RR from current position).
If all 5 keys fail (429 or empty 200) → fallback to kimi_hm_nv tier.
If kimi tier also all-fail → ABORT-NO-FALLBACK (deepseek removed R38.6).

Each tier continues from its current key position (not from k1).
Empty 200 detection: choices=null, content=null, empty choices list.

Chain: hm40006 → LiteLLM 41101-41105 → mihomo per-key proxy → NV API
"""
import json
import http.client
import socket
import time
import urllib.parse

from .config import (
    HM_LITELLM_URLS, HM_NUM_KEYS, HM_LITELLM_KEY,
    NV_MODEL_IDS, NV_MODEL_TIERS, DEFAULT_NV_MODEL, detect_nv_model,
    get_tier_index, litellm_model_name,
    UPSTREAM_TIMEOUT, TIER_TIMEOUT_BUDGET_S,
    _next_hm_nv_key,
    throttle_outbound,
    is_key_cooling, mark_key_cooling, reset_key429_count,
)
from .logger import _log, _log_metrics, _log_error_detail


class UpstreamResult:
    """Result from LiteLLM upstream request execution."""
    def __init__(self):
        self.success = False
        # Success fields
        self.resp = None
        self.conn = None
        self.tier_model = ""
        self.nv_key_idx = 0
        self.nv_model_label = ""
        self.is_stream = False
        self.key_cycle_attempts = []
        self.upstream_type = "nv_litellm"
        self.tier_attempts = []  # R38.2: per-tier attempt summary
        self.fallback_tiers_used = []  # R38.2: which tiers were tried
        # Error fields
        self.all_keys_exhausted = False
        self.all_429 = False
        self.empty_200 = False
        self.elapsed_ms = 0
        self.final_error_json = None
        self.final_resp_status = 0


def _make_litellm_conn(litellm_url, timeout=UPSTREAM_TIMEOUT):
    """Create HTTPConnection to LiteLLM container."""
    parsed = urllib.parse.urlparse(litellm_url)
    host = parsed.hostname
    port = parsed.port or 4000
    path_prefix = parsed.path.rstrip("/")
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn, path_prefix


def _check_empty_200(resp, key_idx, tier_model, is_stream):
    """Check if a 200 response is actually empty (no real content).

    NV API can return 200 with null choices, null content, or empty response.
    These are treated as failures and trigger key cycling or fallback.

    For streaming: don't read body (would break stream). Use Content-Length=0
    as a hint. Stream empty content will be caught in SSE parsing.
    For non-streaming: read body and check choices/content.

    Returns: True if empty 200, False if valid response.
    On valid non-stream: sets resp_body on the resp object for later use.
    """
    content_length_str = resp.getheader("Content-Length", "-1")
    transfer_encoding = resp.getheader("Transfer-Encoding", "")

    if is_stream:
        # Streaming: can't read body. Content-Length=0 is a strong signal.
        if content_length_str == "0":
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 Content-Length:0 (stream)")
            return True
        # Otherwise trust the response — stream will naturally end if empty
        return False

    # Non-streaming: read and inspect body
    resp_body = resp.read()
    # Store body on resp for later retrieval (avoid double-read)
    resp._hm_cached_body = resp_body

    if not resp_body or len(resp_body) == 0:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 empty body (0 bytes)")
        return True

    try:
        oai_resp = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        # Can't parse as JSON — could be garbage, treat as empty
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 unparseable body ({len(resp_body)}b)")
        return True

    choices = oai_resp.get("choices")
    # choices is None/null → empty
    if choices is None:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices=null")
        return True
    # choices is empty list → empty
    if isinstance(choices, list) and len(choices) == 0:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices=[] (empty)")
        return True
    # choices[0] is null → empty
    if isinstance(choices, list) and choices[0] is None:
        _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 choices[0]=null")
        return True
    # choices[0].message.content is null → empty
    if isinstance(choices, list) and len(choices) > 0:
        msg = choices[0].get("message")
        if msg is None:
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 message=null")
            return True
        content = msg.get("content")
        if content is None:
            _log("HM-EMPTY-200", f"k{key_idx+1} ({tier_model}) → 200 content=null")
            return True

    # Valid response with real content
    return False


def _try_tier_keys(oai_body, tier_model, request_id, metrics, t_start,
                   is_stream, prior_cycle_attempts):
    """Try all 5 keys within one tier, starting from current RR position.

    On 429/500/502: cycle to next key within same tier.
    On empty 200: cycle to next key within same tier.
    On other error: report immediately (no cycling).

    R38.5: 429 cooldown restored. Keys that recently got 429 are skipped
    (cooldown duration configurable via KEY_COOLDOWN_S env var).
    Attempt range doubled (HM_NUM_KEYS * 2) to allow cooldown recovery.

    Returns: UpstreamResult
      - success=True: valid response found
      - success=False, empty_200=True: all keys returned empty 200
      - success=False, all_429=True: all keys returned 429
      - success=False: mixed failures within tier
    """
    result = UpstreamResult()
    result.is_stream = is_stream
    result.tier_model = tier_model
    key_cycle_attempts = list(prior_cycle_attempts)

    nv_model_id = NV_MODEL_IDS[tier_model]
    _log("HM-TIER", f"Starting tier={tier_model} model={nv_model_id} "
                    f"(position from rr_counter)")

    # Get starting key from per-tier persistent counter
    start_key_idx = _next_hm_nv_key(tier_model)

    # R38.6: Tier timeout budget — stop trying keys in this tier if cumulative
    # elapsed time exceeds TIER_TIMEOUT_BUDGET_S. Prevents stacking of timeouts
    # (5 keys × 45s each = 225s worst case). Budget of 90s allows ~2 timeout
    # retries; if 2 keys timeout, the tier is likely broken.
    tier_budget_start = time.time()

    # R38.6: Attempt range = HM_NUM_KEYS + 2 (reduced from HM_NUM_KEYS * 2 = 10 → 7).
    # Rationale: with tier timeout budget + correct sock.settimeout(45) + deepseek
    # removed from tiers, max worst case per tier is now 7×45=315s → but budget caps at 90s.
    # Extra 2 beyond HM_NUM_KEYS covers cooldown skip slots.
    for attempt_idx in range(HM_NUM_KEYS + 2):
        key_idx = (start_key_idx + attempt_idx) % HM_NUM_KEYS

        # R38.6: Tier timeout budget check — break if budget exceeded
        elapsed_in_tier = time.time() - tier_budget_start
        if elapsed_in_tier >= TIER_TIMEOUT_BUDGET_S:
            _log("HM-TIER-BUDGET", f"tier={tier_model} budget {TIER_TIMEOUT_BUDGET_S}s "
                                    f"exceeded after {elapsed_in_tier:.1f}s, "
                                    f"breaking to next tier")
            break

        # R38.5: Skip keys in 429 cooldown to avoid wasting requests
        if is_key_cooling(tier_model, key_idx):
            _log("HM-KEY", f"tier={tier_model} k{key_idx+1} is in cooldown (429), skipping")
            # After all keys checked once, if still no success and all are cooling, break to fallback
            if attempt_idx >= HM_NUM_KEYS and all(is_key_cooling(tier_model, k) for k in range(HM_NUM_KEYS)):
                _log("HM-TIER", f"tier={tier_model} all keys in cooldown, breaking to fallback")
                break
            continue

        litellm_url = HM_LITELLM_URLS[key_idx]
        model_label = litellm_model_name(tier_model, key_idx)

        # Build LiteLLM request body
        litellm_body = dict(oai_body)
        litellm_body["model"] = model_label

        _log("HM-KEY", f"tier={tier_model} attempt {attempt_idx+1}/{HM_NUM_KEYS + 2}: "
                       f"k{key_idx+1} → {litellm_url} model={model_label}")

        litellm_data = json.dumps(litellm_body).encode("utf-8")
        headers_out = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {HM_LITELLM_KEY}",
            "Content-Length": str(len(litellm_data)),
            "Connection": "close",  # R38.6: prevent connection reuse → BrokenPipe errors
        }

        try:
            conn, path_prefix = _make_litellm_conn(litellm_url, UPSTREAM_TIMEOUT)
            litellm_path = path_prefix.rstrip("/") + "/chat/completions"
            # R38.5: throttle only on first key attempt of a request, not during key cycling.
            # NV RPM is per-key independent — cycling to a different key has its own RPM bucket,
            # so throttle delay between cycling attempts is pure waste.
            # First attempt still throttled to respect RPM burst limits on the first key.
            if attempt_idx == 0:
                throttle_outbound()
            conn.request("POST", litellm_path, body=litellm_data, headers=headers_out)
            # R38.6 CRITICAL FIX: sock.settimeout() BEFORE getresponse()
            # Previous code had settimeout AFTER getresponse(), which means
            # getresponse() inherited HTTPConnection.timeout (only controls
            # connect phase TCP+SSL, NOT read phase). This caused getresponse()
            # to block indefinitely (no read-side timeout), explaining the
            # 650-770s per-key timeouts observed in Round 2 logs.
            # Fix: set sock.settimeout between request() and getresponse().
            if conn.sock:
                conn.sock.settimeout(UPSTREAM_TIMEOUT)
            resp = conn.getresponse()

            if resp.status >= 400:
                error_body = resp.read()
                try:
                    error_json = json.loads(error_body)
                except Exception:
                    error_json = {"error": error_body.decode("utf-8", errors="replace")}
                conn.close()
                err_str = json.dumps(error_json)

                # Cycling errors: 429/500/502 → next key in same tier
                should_cycle = resp.status in (429, 500, 502)
                if should_cycle:
                    cycle_reason = "429_nv_rate_limit" if resp.status == 429 else \
                                   "500_nv_error" if resp.status == 500 else "502_nv_error"
                    key_cycle_attempts.append({
                        "tier": tier_model,
                        "nv_key_idx": key_idx,
                        "litellm_model": model_label,
                        "error_body": err_str[:500],
                        "error_type": cycle_reason,
                        "upstream_type": "nv_litellm",
                    })
                    # R38.5: Mark key as cooling after 429 to avoid wasting subsequent requests
                    if resp.status == 429:
                        mark_key_cooling(tier_model, key_idx)
                        _log("HM-COOLDOWN", f"tier={tier_model} k{key_idx+1} marked cooling after 429")
                    _log("HM-CYCLE", f"tier={tier_model} k{key_idx+1} ({model_label}) → "
                                     f"{resp.status} ({cycle_reason}), cycling to next key")
                    continue

                # Non-cycling error → report
                result.final_error_json = error_json
                result.final_resp_status = resp.status
                result.key_cycle_attempts = key_cycle_attempts
                result.elapsed_ms = int((time.time() - t_start) * 1000)
                return result

            # ─── 200 response — check for empty ───
            is_empty = _check_empty_200(resp, key_idx, tier_model, is_stream)

            if is_empty:
                # Empty 200 → treat as failure, cycle to next key
                key_cycle_attempts.append({
                    "tier": tier_model,
                    "nv_key_idx": key_idx,
                    "litellm_model": model_label,
                    "error_type": "empty_200",
                    "upstream_type": "nv_litellm",
                })
                _log("HM-EMPTY-CYCLE", f"tier={tier_model} k{key_idx+1} empty 200, cycling to next key")
                # Close connection for empty response
                try:
                    conn.close()
                except Exception:
                    pass
                continue

            # ─── Valid success response ───
            result.success = True
            result.resp = resp
            result.conn = conn
            result.tier_model = tier_model
            result.nv_key_idx = key_idx
            result.nv_model_label = model_label
            result.key_cycle_attempts = key_cycle_attempts
            result.fallback_tiers_used = [tier_model]
            # R38.5: Reset 429 count when key succeeds — cooldown exponential backoff resets
            reset_key429_count(tier_model, key_idx)
            metrics["upstream_type"] = "nv_litellm"
            metrics["tier_model"] = tier_model
            metrics["nv_key_idx"] = key_idx
            metrics["litellm_model"] = model_label
            if key_cycle_attempts:
                metrics["key_cycle_429s_before_success"] = len(key_cycle_attempts)
                metrics["key_cycle_details"] = key_cycle_attempts
                _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} succeeded after "
                                    f"{len(key_cycle_attempts)} cycle attempts")
            else:
                _log("HM-SUCCESS", f"tier={tier_model} k{key_idx+1} succeeded on first attempt")
            return result

        except socket.timeout as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-TIMEOUT", f"tier={tier_model} k{key_idx+1} timeout after {elapsed_ms}ms")
            key_cycle_attempts.append({
                "tier": tier_model,
                "nv_key_idx": key_idx,
                "litellm_model": model_label,
                "error_type": "LiteLLMTimeout",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv_litellm",
            })
            continue

        except (ConnectionRefusedError, http.client.RemoteDisconnected) as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-CONN", f"tier={tier_model} k{key_idx+1} connection error: {e}")
            key_cycle_attempts.append({
                "tier": tier_model,
                "nv_key_idx": key_idx,
                "litellm_model": model_label,
                "error_type": f"LiteLLM{type(e).__name__}",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv_litellm",
            })
            continue

        except Exception as e:
            error_class = type(e).__name__
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log("HM-ERR", f"tier={tier_model} k{key_idx+1} {error_class}: {e}")
            key_cycle_attempts.append({
                "tier": tier_model,
                "nv_key_idx": key_idx,
                "litellm_model": model_label,
                "error": str(e)[:200],
                "error_type": f"LiteLLM{error_class}",
                "elapsed_ms": elapsed_ms,
                "upstream_type": "nv_litellm",
            })
            continue

    # ─── All keys in this tier exhausted ───
    # Classify: all 429, all empty 200, or mixed
    tier_attempts = [a for a in key_cycle_attempts if a.get("tier") == tier_model]
    all_429 = all(a.get("error_type") == "429_nv_rate_limit" for a in tier_attempts)
    all_empty = all(a.get("error_type") == "empty_200" for a in tier_attempts)

    result.all_keys_exhausted = True
    result.all_429 = all_429
    result.empty_200 = all_empty
    result.key_cycle_attempts = key_cycle_attempts
    result.elapsed_ms = int((time.time() - t_start) * 1000)

    fail_summary = f"429={sum(1 for a in tier_attempts if a.get('error_type')=='429_nv_rate_limit')}, " \
                   f"empty200={sum(1 for a in tier_attempts if a.get('error_type')=='empty_200')}, " \
                   f"timeout={sum(1 for a in tier_attempts if 'Timeout' in a.get('error_type',''))}, " \
                   f"other={sum(1 for a in tier_attempts if a.get('error_type') not in ('429_nv_rate_limit','empty_200') and 'Timeout' not in a.get('error_type',''))}"
    _log("HM-TIER-FAIL", f"tier={tier_model} all {HM_NUM_KEYS} keys failed: {fail_summary}, "
                          f"elapsed={result.elapsed_ms}ms")

    # R38.5: When ALL keys in a tier hit 429, mark entire tier for global cooldown.
    # This prevents rapid re-cycling when the tier recovers but then immediately 429s again.
    if all_429:
        for k in range(HM_NUM_KEYS):
            mark_key_cooling(tier_model, k, duration_s=15)  # 15s global tier cooldown
        _log("HM-GLOBAL-COOLDOWN", f"tier={tier_model} all keys 429. Marking all keys cooling 15 seconds")

    # Log error detail for tier failure
    _log_error_detail({
        "request_id": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "error_subcategory": f"tier_{tier_model}_all_keys_failed",
        "tier_model": tier_model,
        "tier_attempts": tier_attempts,
        "all_429": all_429,
        "all_empty_200": all_empty,
        "elapsed_ms": result.elapsed_ms,
    })

    return result


def execute_litellm_request(handler, oai_body, mapped_model, request_id, metrics, t_start):
    """Execute NV request via LiteLLM with three-tier fallback (R38.5).

    R38.6: Tier chain: glm5.1_hm_nv → kimi_hm_nv (deepseek removed, all 30s timeout)
    - mapped_model determines starting tier (default: glm5.1_hm_nv)
    - Each tier tries 5 keys with per-tier persistent RR counter
    - On tier all-fail: fallback to next tier (from current position)
    - All 3 tiers fail: ABORT-NO-FALLBACK
    """
    # Determine starting tier
    start_tier_idx = get_tier_index(mapped_model)
    is_stream = oai_body.get("stream", False)

    _log("HM-REQ", f"mapped_model={mapped_model} start_tier={NV_MODEL_TIERS[start_tier_idx]} "
                   f"stream={is_stream} tier_chain={NV_MODEL_TIERS[start_tier_idx:]}")

    all_attempts = []
    all_tier_summaries = []
    fallback_tiers_used = []

    for tier_idx in range(start_tier_idx, len(NV_MODEL_TIERS)):
        tier_model = NV_MODEL_TIERS[tier_idx]
        is_first_tier = (tier_idx == start_tier_idx)

        # R38.5 Tier Skip: if ALL keys in this tier are in cooldown,
        # skip the entire tier immediately instead of wasting time
        # trying each key (which just gets skipped individually).
        # Data shows: "all keys in cooldown, breaking to fallback" takes 5ms,
        # but first-request tier cycling still wastes ~10s per 429.
        # With skip: 0ms overhead, directly to next tier.
        all_cooling = all(is_key_cooling(tier_model, k) for k in range(HM_NUM_KEYS))
        if all_cooling:
            _log("HM-TIER-SKIP", f"tier={tier_model} all {HM_NUM_KEYS} keys in cooldown, "
                                  f"skipping entire tier → next tier")
            # Record as tier failure for metrics
            all_tier_summaries.append({
                "tier": tier_model,
                "all_429": True,
                "all_empty_200": False,
                "num_attempts": 0,
                "elapsed_ms": 0,
                "skipped": True,
            })
            if not is_first_tier:
                _log("HM-FALLBACK", f"Tier {NV_MODEL_TIERS[tier_idx-1]} all-failed → "
                                    f"falling back to {tier_model} (continuing from current position)")
            continue

        if not is_first_tier:
            _log("HM-FALLBACK", f"Tier {NV_MODEL_TIERS[tier_idx-1]} all-failed → "
                                f"falling back to {tier_model} (continuing from current position)")

        tier_result = _try_tier_keys(oai_body, tier_model, request_id, metrics, t_start,
                                     is_stream, all_attempts)

        if tier_result.success and not tier_result.empty_200:
            # ─── Success at this tier ───
            # Merge tier summary info
            tier_result.fallback_tiers_used = [NV_MODEL_TIERS[i] for i in range(start_tier_idx, tier_idx + 1)]
            if not is_first_tier:
                _log("HM-FALLBACK-SUCCESS", f"Success on fallback tier {tier_model} after "
                                            f"primary {NV_MODEL_TIERS[start_tier_idx]} failed "
                                            f"(tried tiers: {tier_result.fallback_tiers_used})")
                metrics["fallback_from"] = NV_MODEL_TIERS[tier_idx - 1]
                metrics["fallback_to"] = tier_model
            # Update metrics with tier info
            metrics["tier_model"] = tier_result.tier_model
            metrics["fallback_tiers_used"] = tier_result.fallback_tiers_used
            return tier_result

        # ─── Tier all-failed: record and try next ───
        tier_attempts = [a for a in tier_result.key_cycle_attempts
                         if a.get("tier") == tier_model or a not in all_attempts]
        all_tier_summaries.append({
            "tier": tier_model,
            "all_429": tier_result.all_429,
            "all_empty_200": tier_result.empty_200,
            "num_attempts": len(tier_attempts),
            "elapsed_ms": tier_result.elapsed_ms,
        })
        all_attempts = list(tier_result.key_cycle_attempts)

        # Close any remaining connections from failed tier
        if tier_result.conn:
            try:
                tier_result.conn.close()
            except Exception:
                pass

    # ─── All 3 tiers exhausted ───
    _log("HM-ALL-TIERS-FAIL", f"All {len(NV_MODEL_TIERS)-start_tier_idx} tiers failed "
                               f"(tiers tried: {NV_MODEL_TIERS[start_tier_idx:]}), "
                               f"elapsed={int((time.time() - t_start) * 1000)}ms, ABORT-NO-FALLBACK")

    # Determine overall classification
    has_429 = any(s.get("all_429") for s in all_tier_summaries)
    has_empty = any(s.get("all_empty_200") for s in all_tier_summaries)

    final_result = UpstreamResult()
    final_result.success = False
    final_result.all_keys_exhausted = True
    final_result.all_429 = has_429 and not has_empty  # Pure 429 if no empty 200
    final_result.empty_200 = has_empty
    final_result.key_cycle_attempts = all_attempts
    final_result.tier_attempts = all_tier_summaries
    final_result.fallback_tiers_used = [NV_MODEL_TIERS[i] for i in range(start_tier_idx, len(NV_MODEL_TIERS))]
    final_result.elapsed_ms = int((time.time() - t_start) * 1000)
    final_result.final_resp_status = 429 if has_429 else 502

    # Log comprehensive error detail
    _log_error_detail({
        "request_id": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "error_subcategory": "all_tiers_failed",
        "start_tier": NV_MODEL_TIERS[start_tier_idx],
        "tiers_tried": NV_MODEL_TIERS[start_tier_idx:],
        "tier_summaries": all_tier_summaries,
        "total_attempts": len(all_attempts),
        "elapsed_ms": final_result.elapsed_ms,
    })

    _log_metrics({
        "request_id": request_id,
        "error_subcategory": "all_tiers_failed",
        "start_tier": NV_MODEL_TIERS[start_tier_idx],
        "tiers_tried": final_result.fallback_tiers_used,
        "total_cycle_attempts": len(all_attempts),
        "elapsed_ms": final_result.elapsed_ms,
    })

    return final_result
