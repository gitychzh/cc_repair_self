# R23: Multi-Agent Gateway Refactoring Plan

## Objective
1. Add model ID suffixes: `_cc` (Claude Code), `_ol` (OpenClaw), `_oc` (OpenCode), `_hm` (Hermes)
2. Refactor proxy: extract v×k cycling + error handling into shared `upstream.py`
3. OpenAI-format agents (`_ol/_oc/_hm`) get **full cycling** (429/500/502 key cycling, timeout cycling)
4. Debug each agent one-by-one on remote opc_uname: CC → OpenClaw → OpenCode → Hermes

## Design Decisions (Confirmed by User)

1. **CC uses `claude-opus-4-8` → MODEL_MAP routes to `glm5.1` (internally tagged as `_cc` agent type)**
   - `claude-opus-4-8` → `glm5.1` (agent_type="cc") — CC backward compat
   - `/v1/models` Anthropic format shows `glm5.1_cc` as available model ID
   - CC can also request `glm5.1_cc` directly (MODEL_MAP maps it)

2. **OpenAI-format agents use `/v1/chat/completions` endpoint**
   - `_ol` (OpenClaw): OpenAI request/response format, full v×k cycling
   - `_oc` (OpenCode): OpenAI request/response format, full v×k cycling  
   - `_hm` (Hermes): OpenAI request/response format, full v×k cycling

3. **Only glm5.1 model for now; dsv4p is reserved (not focused)**

4. **Suffix = agent type marker** (determines response format + metrics tagging)
   - `glm5.1_cc` → Anthropic response format, CC error types
   - `glm5.1_ol/oc/hm` → OpenAI response format, OpenAI error types

## Architecture: Modular Decoupling

### New Module: `gateway/upstream.py` — Shared v×k Cycling Executor

Extract the entire v×k 2D round-robin + error cycling logic from `handlers.py._handle_messages()`:

```python
class UpstreamResult:
    """Result of upstream request execution with v×k cycling."""
    success: bool
    resp: http.client.HTTPResponse  # only if success
    conn: http.client.HTTPConnection  # only if success  
    # Error info (only if !success)
    all_429: bool
    has_500: bool
    has_502: bool
    has_timeout: bool
    has_conn_err: bool
    cycle_attempts: list[dict]
    error_json: dict  # last error JSON from upstream
    resp_status_final: int
    # Metadata
    key_idx: int
    variant_idx: int
    litellm_model: str
    is_input_overflow: bool
    is_quota_exhaustion: bool

def execute_request(handler, oai_body, mapped_model, request_id, metrics, t_start) -> UpstreamResult
```

This function encapsulates:
- v×k 2D round-robin pair selection
- Key cycling loop (429/500/502 → try next key in same variant)
- socket.timeout → cycle to next key
- Connection errors → cycle to next key
- Thinking_budget fix retry
- Resilience retry (401/403 AuthenticationError)
- All-keys-exhausted classification
- Returns structured result so handlers can format per agent type

### Modified Module: `gateway/handlers.py` — Slim Dispatcher

**Current**: `_handle_messages()` is 570 lines with cycling logic deeply embedded
**After**: `_handle_messages()` becomes ~80 lines (parse → convert → call upstream → format response)

```python
# _handle_messages() — Anthropic (CC/_cc) requests
def _handle_messages(self):
    # Parse Anthropic request body
    # Convert Anthropic → OpenAI (anth_to_openai)
    # Force-stream for non-stream (ModelScope delta bug)
    # Call upstream.execute_request(handler, oai_body, mapped_model, ...)
    # If success: stream/collect/direct → Anthropic response format
    # If error: format as Anthropic error (rate_limit_error/api_error/invalid_request_error)

# _handle_openai_with_cycling() — OpenAI (_ol/_oc/_hm) requests
def _handle_openai_with_cycling(self):
    # Parse OpenAI request body
    # Detect agent type from model suffix (_ol/_oc/_hm)
    # Strip suffix → mapped_model
    # Call upstream.execute_request(handler, oai_body, mapped_model, ...)
    # If success: pass through OpenAI response directly
    # If streaming: pass through SSE stream directly
    # If error: format as OpenAI error format
```

### Modified Module: `gateway/config.py` — Agent Type Config

```python
# Agent suffix configuration
AGENT_SUFFIXES = {
    "_cc": {"name": "Claude Code", "format": "anthropic"},
    "_ol": {"name": "OpenClaw", "format": "openai"},
    "_oc": {"name": "OpenCode", "format": "openai"},
    "_hm": {"name": "Hermes", "format": "openai"},
}

def detect_agent_type(model_id: str) -> tuple[str, str]:
    """Strip suffix → return (base_model, agent_type).
    e.g. "glm5.1_ol" → ("glm5.1", "ol")
    e.g. "glm5.1" → ("glm5.1", "cc")  # default = CC
    e.g. "claude-opus-4-8" → ("glm5.1", "cc")  # via MODEL_MAP
    """

# MODEL_MAP updates
MODEL_MAP = {
    # CC backward compat — no suffix = CC
    "glm5.1": "glm5.1", "glm-5.1": "glm5.1",
    "glm5.1_cc": "glm5.1",  # explicit CC suffix
    # OpenAI-format agents
    "glm5.1_ol": "glm5.1",  # OpenClaw
    "glm5.1_oc": "glm5.1",  # OpenCode
    "glm5.1_hm": "glm5.1",  # Hermes
    # dsv4p (reserved, not focused now)
    "dsv4p": "dsv4p", "dsv4p_cc": "dsv4p",
    "dsv4p_ol": "dsv4p", "dsv4p_oc": "dsv4p", "dsv4p_hm": "dsv4p",
    # Claude names → glm5.1 (CC)
    "claude-opus-4-8": "glm5.1",
    # ... rest unchanged
}
```

### Modified Module: `gateway/error_mapping.py` — Add OpenAI Error Formatting

```python
def format_anthropic_error(result: UpstreamResult, request_model) -> dict:
    """Format error as Anthropic error type (for CC/_cc)."""
    # Existing convert_error logic, but using UpstreamResult fields
    
def format_openai_error(result: UpstreamResult, request_model) -> dict:
    """Format error as OpenAI error format (for _ol/_oc/_hm)."""
    # OpenAI error format: {"error": {"message": "...", "type": "...", "code": "..."}}
    # All 429 → {"error": {"type": "insufficient_quota", "code": "429"}}
    # Mixed 500/502/timeout → {"error": {"type": "server_error", "code": "502"}}
    # Input overflow → {"error": {"type": "invalid_request_error", "code": "400"}}
```

### Model List Endpoints Update

- `_anthropic_models_list()`: Show `glm5.1_cc`, `dsv4p_cc` + Claude names (claude-opus-4-8 → display_name="glm5.1_cc")
- `_proxy_models()`: Show `glm5.1_ol`, `glm5.1_oc`, `glm5.1_hm`, `dsv4p_ol/oc/hm`
- Each suffix model ID is a valid requestable name via MODEL_MAP

## Implementation Steps (Sequential, One Agent at a Time)

### Step 1: Create `gateway/upstream.py`
- Extract v×k cycling logic from `handlers.py._handle_messages()` 
- Create `UpstreamResult` dataclass
- Create `execute_request()` function with full cycling support
- Support both Anthropic (force_stream_for_nonstream) and OpenAI (no force-stream) modes via parameter

### Step 2: Refactor `gateway/handlers.py`
- Slim down `_handle_messages()` to use `upstream.execute_request()`
- Replace `_passthrough_openai()` with `_handle_openai_with_cycling()`
- Add agent type detection in `do_POST()` routing
- Update `do_POST()`: model suffix determines which handler to use

### Step 3: Update `gateway/config.py`
- Add `AGENT_SUFFIXES` dict
- Add `detect_agent_type()` function  
- Update `MODEL_MAP` with suffix entries
- Add `get_agent_format()` helper

### Step 4: Update `gateway/error_mapping.py`
- Add `format_anthropic_error()` using `UpstreamResult`
- Add `format_openai_error()` for OpenAI-format agents
- Refactor `convert_error()` to use `UpstreamResult` fields

### Step 5: Update model list endpoints
- `_anthropic_models_list()`: include `_cc` suffix models
- `_proxy_models()`: include `_ol/_oc/_hm` suffix models

### Step 6: Deploy + Test CC (`_cc`) — Backward compat verification
- Deploy to opc_uname remote
- Test: `curl -X POST http://127.0.0.1:40001/v1/messages -H "anthropic-version: 2023-06-01" -d '{"model":"glm5.1","messages":[{"role":"user","content":"test"}],"max_tokens":50}'`
- Test: `curl -X POST http://127.0.0.1:40001/v1/messages -H "anthropic-version: 2023-06-01" -d '{"model":"glm5.1_cc","messages":[{"role":"user","content":"test"}],"max_tokens":50}'`
- Verify: `claude-opus-4-8` still routes correctly

### Step 7: Debug OpenClaw (`_ol`) — First OpenAI agent
- Test: `curl -X POST http://127.0.0.1:40001/v1/chat/completions -d '{"model":"glm5.1_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}'`
- Verify: streaming works, non-stream works, error cycling works
- Verify: OpenAI error format on 429/500/502

### Step 8: Debug OpenCode (`_oc`) — Second OpenAI agent
- Test: `curl -X POST http://127.0.0.1:40001/v1/chat/completions -d '{"model":"glm5.1_oc","messages":[{"role":"user","content":"test"}],"max_tokens":50}'`

### Step 9: Debug Hermes (`_hm`) — Third OpenAI agent
- Test: `curl -X POST http://127.0.0.1:40001/v1/chat/completions -d '{"model":"glm5.1_hm","messages":[{"role":"user","content":"test"}],"max_tokens":50}'`

### Step 10: Update docs (CLAUDE.md, DEPLOY_STATUS.md)

## File Changes Summary

| File | Action | Key Changes |
|------|--------|-------------|
| `gateway/upstream.py` | **NEW** | UpstreamResult + execute_request() — shared v×k cycling |
| `gateway/config.py` | MODIFY | AGENT_SUFFIXES, detect_agent_type(), MODEL_MAP suffixes |
| `gateway/handlers.py` | MODIFY | Use upstream.py, add _handle_openai_with_cycling(), slim down _handle_messages() |
| `gateway/error_mapping.py` | MODIFY | format_anthropic_error(), format_openai_error() |
| `gateway/__init__.py` | MODIFY | Add upstream module |
| `CLAUDE.md` | MODIFY | Document suffix IDs, agent routing, upstream module |
| `DEPLOY_STATUS.md` | MODIFY | R23 status |

## Risks & Mitigations
1. **CC backward compat**: `claude-opus-4-8` and `glm5.1` (no suffix) still work → MODEL_MAP unchanged for these entries, default agent_type="cc"
2. **Force-stream only for CC**: OpenAI agents get proper non-stream responses; force-stream only applies to Anthropic format (ModelScope 'delta' bug)
3. **v×k counter sharing**: All agent types share same per-model counter → correct, same ModelScope quota pool
4. **OpenAI streaming passthrough**: For `_ol/_oc/_hm` streaming requests, v×k cycling happens on initial connection, then SSE stream passes through directly (no format conversion needed)
