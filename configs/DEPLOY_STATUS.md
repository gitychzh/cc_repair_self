# Deploy Status — opc_uname (2026-05-31)

## Architecture
```
CC → 40001(proxy, format conversion only) → 41001(LiteLLM glm5.1) → ModelScope
                                          → 42001(LiteLLM dsv4p)  → ModelScope
```

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (77 deployments: 11 variants × 7 keys, rpm=1)
- dsv4p_uni42001 :42001 (77 deployments: 11 variants × 7 keys, rpm=1)
- auth_to_api_40001 :40001 (proxy, 784 lines)
- auth_to_api_40002 :40002 (Codex proxy, framework only)

## Router Settings (aligned with local stable config)
- num_retries: 3
- cooldown_time: 30
- routing_strategy: latency-based-routing
- lowest_latency_buffer: 0.1
- rolling_window_size: 10
- RateLimitErrorAllowedFails: 3
- TimeoutErrorAllowedFails: 2

## Test Results (2026-05-31)
- glm5.1 OK: Anthropic format, thinking + text
- dsv4p OK: Anthropic format, thinking + text
- 41001 models: glm5.1 only
- 42001 models: dsv4p only

## Round 2 Changes — opc_uname optimizing opc2_uname (2026-05-31)

### CC Settings on opc2_uname (~/.claude/settings.json)
| Parameter | Before | After | Reason |
|-----------|--------|-------|--------|
| contextWindow | 190000 | 120000 | GLM-5.1 real capacity = 128K (131072 tokens). 120K ≈ 91% of 131K, auto-compact triggers BEFORE model errors |
| CLAUDE_CODE_MAX_OUTPUT_TOKENS | 32768 | 8192 | Model internally caps at ~4-8K. 8192 gives room for longer responses without wasting quota on impossible 32K requests |
| autoCompactWindow | not set | "auto" | Claude Code v2.1.158 feature. Actual threshold = min(auto, contextWindow). Ensures graceful compaction |

### TUI StatusLine on opc2_uname (~/.claude/statusline-command.sh)
- Added statusLine feature: displays model name + token count + context usage % in TUI bottom bar
- Example display: `glm5.1 | 10222/200000 tokens (5% used)`
- Uses jq to parse JSON from Claude Code stdin (model.display_name, context_window stats)

### Port Correction: 41002→42001
- dsv4p port: 41002→42001 (41002 was input error, corrected to 42001)
- Container: dsv4p_uni41002→dsv4p_uni42001
- proxy internal URL: dsv4p_uni41002→dsv4p_uni42001

### Documentation Sources
- GLM-5.1 context window: 128K (131072 tokens) — ZhipuAI open.bigmodel.cn API docs, ModelScope model card
- autoCompactWindow/statusLine: reverse-engineered from Claude Code v2.1.158 binary