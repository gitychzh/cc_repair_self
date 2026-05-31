# Deploy Status — opc_uname (2026-05-31)

## Architecture
```
CC → 40001(proxy, format conversion only) → 41001(LiteLLM glm5.1) → ModelScope
                                          → 41002(LiteLLM dsv4p)  → ModelScope
```

## Containers (all healthy)
- cc_postgres :5432
- glm5.1_uni41001 :41001 (77 deployments: 11 variants × 7 keys, rpm=1)
- dsv4p_uni41002 :41002 (77 deployments: 11 variants × 7 keys, rpm=1)
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
- 41002 models: dsv4p only