# R9: Systematic 529 Overloaded Crash Fix — Deploy R8 + Parameter Tuning

## Root Cause Analysis

The "Repeated 529 Overloaded" crash has TWO independent causes:

### Cause 1: INPUT-REJECT 529 — R8 auto-compact NOT deployed (47 occurrences/day)
- **Repository** has R8 proxy.py (1675 lines) with `_auto_compact_messages()` that converts INPUT-REJECT from 529 → auto-compact + forward
- **Remote machine** still runs OLD proxy.py (1542 lines) that returns 529 on INPUT-REJECT
- Result: borderline requests (est_tokens=120088, barely over 120K) → 529 → CC retries 3 times → crash
- Data: 47 INPUT-REJECT events, ALL with est_tokens just barely over 120K threshold
- ModelScope actual limit = 202745, these borderline requests could succeed at upstream

### Cause 2: 429 quota-exhausted → CC immediate retry (119 occurrences/day)
- ALL 429 errors are "exceeded your current quota" (account-level quota exhaustion)
- Currently mapped to `api_error` → CC retries immediately (no backoff)
- CC generates more 429s in a tight loop → 12.5% of all requests wasted
- Should be `rate_limit_error` → CC exponential backoff (seconds→minutes→hours)

### Why CC crashes on 529:
- CC gets 529 overloaded_error → retries 3 times with SAME context (no auto-compact)
- 3 consecutive 529s → CC shows "Repeated 529 Overloaded" and freezes/crashes
- CC only auto-compacts AFTER the 3rd 529 (too late, already crashed)
- The R8 auto-compact approach breaks this loop by compacting IN THE PROXY

## Fix Plan (4 changes)

### Fix A: Deploy R8 auto-compact proxy.py to remote machine
- Copy repo proxy.py (with `_auto_compact_messages`) to `/opt/cc-infra/proxy/proxy.py`
- Rebuild proxy container: `docker compose up -d --build --force-recreate auth_to_api_40001`
- This eliminates the INPUT-REJECT → 529 → CC crash loop entirely
- Borderline requests get auto-compacted in proxy → forwarded to LiteLLM → 200 response → CC continues

### Fix B: Map 429 quota-exhausted to rate_limit_error (not api_error)
- Change `_convert_error()` mapping for "exceeded your current quota" from `api_error` to `rate_limit_error`
- CC gets 429 + rate_limit_error → exponential backoff (5s→10s→20s→40s→...)
- This stops CC from immediately retrying on quota exhaustion
- Reduces wasted 429 retries from tight loop to graceful backoff

### Fix C: Raise MODEL_INPUT_TOKEN_SAFETY from 120K to 170K
- docker-compose.yml: `MODEL_INPUT_TOKEN_SAFETY_GLM51: "170000"` and `MODEL_INPUT_TOKEN_SAFETY_DSV4P: "170000"`
- ModelScope actual limit = 202K, so 170K safety gives 32K margin (plenty of room)
- Current 120K threshold rejects borderline 120K requests that could succeed at upstream
- With 170K: only genuinely oversized requests (>170K est_tokens) trigger auto-compact
- Data validation: 35 of 47 INPUT-REJECTs would NOT be rejected at 170K threshold
- Remaining 12 genuinely oversized requests get auto-compacted → succeed at ~50K est_tokens

### Fix D: Adjust CC settings and /v1/models context_window
- settings: `contextWindow: 130000`, `autoCompactWindow: 90000` (unchanged)
- /v1/models endpoint: report `context_window: 170000` (matching safety)
- Current context_window reporting = 120K (too small, causes CC to compact unnecessarily early)
- With context_window=170K: CC has more room, compacts when truly needed

## Deployment Steps

1. Update configs/proxy/proxy.py — Fix B (quota→rate_limit_error) + Fix C context_window
2. Update configs/docker-compose.yml — Fix C (safety 120K→170K)
3. Update configs/claude/settings-opc_uname.json — Fix D (contextWindow 110K→130K)
4. Push to GitHub
5. SSH to opc_uname: git pull, copy configs to /opt/cc-infra/
6. Rebuild proxy container (docker compose up -d --build --force-recreate auth_to_api_40001)
7. Copy settings to ~/.claude/settings.json, restart CC
8. Test with curl (glm5.1 + dsv4p)
9. Verify new metrics — INPUT-REJECT/INPUT-OVERLIMIT drops to near-zero

## Validation

- Before: 47 INPUT-REJECT/day, 119 quota 429/day, CC crashes multiple times
- After: ~12 INPUT-OVERLIMIT/day (only genuinely oversized), auto-compact handles them gracefully
- CC should NEVER see "Repeated 529 Overloaded" crash again
- 429 quota errors get CC exponential backoff → fewer wasted requests