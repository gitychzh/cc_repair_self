# R8: 529 Overloaded Crash — Complete Fix Plan

## Root Cause (Deep Analysis)

The "Repeated 529 Overloaded errors" crash is caused by a **4-layer cascade**:

### Layer 1: CC hardcodes 200K contextWindow for claude-opus-4-8
- CC settings has `model="glm5.1"` + `contextWindow=110000` + `autoCompactWindow=90000`
- BUT: CC's system prompt says "powered by claude-opus-4-8" → CC sends `model="claude-opus-4-8"`
- CC **hardcodes** contextWindow=200K for known Claude models (ignores settings.json)
- CC auto-compacts at 50% of 200K = 100K (NOT the configured 90K)
- Data proof: 249 requests with model="claude-opus-4-8", only 114 with "glm5.1"

### Layer 2: Compaction too late → context exceeds 120K safety limit
- CC compacts at 100K → after compaction, context still ~100K tokens
- But est_tokens for a 100K-token context = ~120K (with overhead from JSON encoding, tools)
- Proxy INPUT-REJECT at est_tokens > 120000 → 529 overloaded_error
- Data proof: 32 INPUT-REJECT 529s, est_tokens ranging 110K-182K

### Layer 3: 529 cascade → CC "Repeated Overloaded" crash
- CC receives 529 → triggers auto-compaction retry → 3x retry burst
- After 3x retries all still 529 (same content, same estimation) → CC crashes
- Data proof: every 529 group has exactly 3 consecutive attempts (0.6s, 1.1s gaps)

### Layer 4: 429 insufficient_quota → HTTP 429 → CC rate-limit backoff
- ModelScope quota exhausted → LiteLLM 429 "exceeded your current quota"
- proxy `_convert_error()` correctly maps to `api_error` type
- BUT HTTP status is still 429 → CC treats 429 as rate_limit regardless of body
- CC enters rate-limit backoff → wastes time → context grows further → worse 529 cascade
- Data proof: 41 429s, ALL are insufficient_quota (0 RPM throttles)

## Fix Strategy — 3 Changes

### Fix 1: proxy.py — insufficient_quota 429 → HTTP 500 (not 429)

**Problem**: CC sees HTTP 429 → always rate-limit backoff, even for quota exhaustion.
**Fix**: In `_get_upstream_status_for_client()`, detect insufficient_quota and return 500 instead of 429.
**Why**: CC treats 500+api_error as server error → simple retry (no backoff, no compaction). This is correct for quota exhaustion (quota resets daily, backoff doesn't help).
**Implementation**: Add a check in the error handling path. If the upstream error contains "exceeded your current quota" or "insufficient_quota", return HTTP 500 instead of passing through 429.

### Fix 2: proxy.py — INPUT-REJECT 529 → add "force_compact" hint + retry cooldown

**Problem**: 529 triggers CC compaction retry 3x in rapid succession (0.6s, 1.1s, 1.2s), but compaction hasn't happened yet → same content → same rejection → crash.
**Fix**: Two changes:
  a. Add `retry-after` header in 529 response (e.g., 5 seconds) to slow CC's retry attempts, giving compaction time to work.
  b. In the 529 message body, explicitly mention "context_window: 120000" so CC can self-adjust.
**Why**: Current 529 returns instantly with no delay guidance → CC retries immediately with same content. Adding retry-after gives compaction a chance to happen before retry. The context_window hint may help CC's newer versions respect the limit.

### Fix 3: CC settings — lower autoCompactWindow to 70000

**Problem**: CC thinks contextWindow=200K (hardcoded for claude-opus-4-8), auto-compacts at 100K.
**Fix**: Set `autoCompactWindow=70000` explicitly in settings.json + env var `CLAUDE_CODE_AUTO_COMPACT_WINDOW=70000`.
**Why**: 
  - If CC respects the env var → compacts at 70K → after compact, context ~50-60K → next request est_tokens ~60-70K < 120K ✓
  - If CC doesn't respect env var → still compacts at 100K (50% of 200K) → after compact ~100K → est_tokens ~120K
  - So this fix only works IF CC respects the env var. But we MUST set it anyway.
  - The real fix for this layer is Fix 2 (529 response guidance).

**Combined effect**: 
- Fix 1 eliminates the 429→backoff→context growth path
- Fix 2 gives CC time to compact before retrying, reducing 529 cascade
- Fix 3 lowers compaction threshold (if CC respects it), preventing context from reaching 120K in the first place

## Files to Change

1. `configs/proxy/proxy.py` — Fix 1 (insufficient_quota → 500) + Fix 2 (retry-after + context_window hint)
2. `configs/claude/settings-opc_uname.json` — Fix 3 (autoCompactWindow 90K→70K)
3. `configs/claude/settings-opc2_uname.json` — Same Fix 3 for opc2_uname
4. `configs/docker-compose.yml` — Update env vars (CLAUDE_CODE_AUTO_COMPACT_WINDOW → 70000 if needed)

## Implementation Details

### proxy.py changes:

**_handle_messages() INPUT-REJECT section** (~line 587-596):
- Add `retry-after` header: 5 seconds
- Add `context_window: 120000` in 529 response body

**Error handling path** (~line 651-878):
- After detecting insufficient_quota in error_json, set `resp_status_final = 500` for the client
- Keep `_convert_error()` mapping as api_error (already correct)
- The `_get_upstream_status_for_client()` method needs to accept the error_json as parameter so it can detect insufficient_quota

### settings changes:
- `autoCompactWindow`: 90000 → 70000
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW`: "90000" → "70000" in env section

## Risk Assessment

- Fix 1 (429→500): Low risk. CC treats 500+api_error as normal server error → retry without backoff. This is CORRECT for quota exhaustion (which is what all our 429s are).
- Fix 2 (retry-after): Low risk. CC may or may not respect retry-after for 529, but even if ignored, the hint doesn't hurt. If respected, it prevents the 0.6s/1.1s rapid retry cascade.
- Fix 3 (autoCompact 70K): Medium risk. More frequent compaction means more context loss, but prevents the fatal 529 cascade that crashes CC entirely. 70K still gives good context depth for most tasks.

## Expected Outcome

- 429 insufficient_quota → CC retries normally (no backoff) → less time wasted → less context growth
- 529 INPUT-REJECT → CC waits 5s before retry → compaction has time to work → fewer cascade 529s
- autoCompactWindow=70K → CC compacts earlier (if respected) → est_tokens stays below 120K → no INPUT-REJECT at all