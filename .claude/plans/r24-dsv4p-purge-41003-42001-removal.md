# R24: CC settings update + opc2_uname sync + 41003/42001 removal + dsv4p purge

## Overview
5 operations on BOTH machines (opc_uname remote + opc2_uname local):

1. CC settings.json model → `glm5.1_cc`
2. Sync R23.1 gateway to opc2_uname local
3. Remove 41003/42001 containers + all related files
4. Remove dsv4p from 41001 LiteLLM + proxy config
5. Update CLAUDE.md + DEPLOY_STATUS.md + push

## Current State (both machines identical)
- 6 containers running: cc_postgres, ms_uni41001, auth_to_api_40001/40002, glm5.1_test41003, dsv4p_uni42001
- ms_uni41001 has 140 dep (70 glm5.1 + 70 dsv4p)
- proxy config.py has MODEL_UPSTREAMS for glm5.1 + dsv4p, DSV4P_VARIANT_IDS, etc.
- CC settings.json model="glm5.1" (backward compat works, but explicit suffix preferred)
- opc_uname: R23.1 gateway deployed; opc2_uname: still old gateway

## Execution Plan

### Step 1: CC settings.json → model="glm5.1_cc" (both machines)
- Edit `~/.claude/settings.json` on both machines
- Change `"model": "glm5.1"` → `"model": "glm5.1_cc"`
- Also update repo configs: `settings-opc_uname.json` and `settings-opc2_uname.json`

### Step 2: Sync R23.1 gateway to opc2_uname
- Copy `configs/proxy/gateway/` files to `/opt/cc-infra/proxy/gateway/`
- Rebuild + recreate proxy containers: `docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002`

### Step 3: Remove 41003/42001 containers + cleanup (both machines)
On each machine:
1. Stop containers: `docker stop glm5.1_test41003 dsv4p_uni42001`
2. Remove containers: `docker rm glm5.1_test41003 dsv4p_uni42001`
3. Remove config dirs: `rm -rf /opt/cc-infra/litellm-glm51-test/ /opt/cc-infra/litellm-dsv4p/`
4. Remove log dirs: `rm -rf /opt/cc-infra/logs/litellm-glm51-test/ /opt/cc-infra/logs/litellm-dsv4p/`
5. Remove postgres DBs: Drop litellm_glm51_test and litellm_dsv4p databases from cc_postgres
6. Remove container service definitions from docker-compose.yml (on both machines)
7. Clean up cc_postgres POSTGRES_MULTIPLE_DATABASES: remove `litellm_dsv4p,litellm_glm51_test`

### Step 4: Remove dsv4p from 41001 + proxy config (both machines)
**LiteLLM config.yaml** (41001 ms_uni41001):
- Remove ALL 70 dsv4p deployments (lines 2013-3840)
- Keep only 70 glm5.1 deployments (lines 46-2012)
- Update header comments: 7 groups × 10 variants = 70 dep (glm5.1 only)
- Remove dsv4p variant ID comments

**proxy/gateway/config.py**:
- Remove `"dsv4p"` from MODEL_UPSTREAMS dict
- Remove `"dsv4p"` from BASE_MODELS list → `["glm5.1"]`
- Remove all dsv4p MODEL_MAP entries: `"dsv4p_cc":"dsv4p", "dsv4p_ol":"dsv4p", "dsv4p_oc":"dsv4p", "dsv4p_hm":"dsv4p"`
- Remove: `"dsv4p": "dsv4p", "deepseek-v4-pro": "dsv4p", "deepseek-ai/deepseek-v4-pro": "dsv4p"`
- Remove haiku→dsv4p mappings: `"claude-haiku-4-5": "dsv4p", "claude-haiku-4-5-20251001": "dsv4p", "claude-3-5-haiku-20241022": "dsv4p"`
- Remove dsv4p OpenAI aliases: `"gpt-4o-mini": "dsv4p", "o3-mini": "dsv4p", "o4-mini": "dsv4p", "gpt-4.1-mini": "dsv4p", "gpt-4.1-nano": "dsv4p"`
- Change haiku/mini aliases to glm5.1 (all agents use same backend now)
- Remove DSV4P_VARIANT_IDS list + DSV4P_VARIANT_IDS from VARIANT_IDS dict
- Remove NUM_VARIANTS_DSV4P env var, NUM_VARIANTS dict → just NUM_VARIANTS_GLM51 directly
- Remove `"dsv4p"` from THINKING_SUPPORT → just `{"glm5.1": True}`
- Remove `"dsv4p"` from MODEL_MAX_INPUT_TOKENS, MODEL_INPUT_TOKEN_SAFETY
- Remove `"dsv4p"` from _vk_rr_counter tracking (counter only has glm5.1 now)

**docker-compose.yml env vars**:
- Remove `LITELLM_URL_DSV4P` and `LITELLM_MODELS_URL_DSV4P` from both proxy containers
- Remove `MODEL_INPUT_TOKEN_SAFETY_DSV4P` from both proxy containers
- Remove `NUM_VARIANTS_DSV4P` from both proxy containers

**After config changes**:
- Restart ms_uni41001: `docker restart ms_uni41001`
- Rebuild proxy: `docker compose up -d --build --force-recreate auth_to_api_40001 auth_to_api_40002`

### Step 5: Update docs + push
- Update CLAUDE.md:
  - Remove dsv4p from architecture diagram
  - Remove dsv4p variant model IDs section
  - Remove NUM_VARIANTS_DSV4P from params table
  - Remove dsv4p-related constraints
  - Update container list (4 containers only)
  - Update LiteLLM description (7 groups × 10 variants = 70 dep)
  - Remove dsv4p suffix IDs from frontend model_name constraint
- Update DEPLOY_STATUS.md with R24 changes
- Git push

### Verification (after all changes)
- Test CC path: `curl -s -X POST http://127.0.0.1:40001/v1/messages -H "Content-Type: application/json" -H "x-api-key: sk-litellm-local" -H "anthropic-version: 2023-06-01" -d '{"model":"glm5.1_cc","messages":[{"role":"user","content":"test"}],"max_tokens":50}'`
- Test OpenAI path: `curl -s -X POST http://127.0.0.1:40001/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer sk-litellm-local" -d '{"model":"glm5.1_ol","messages":[{"role":"user","content":"test"}],"max_tokens":50}'`
- Verify dsv4p model returns error (no longer supported): `curl ... model=dsv4p`
- Verify 41003/42001 containers gone: `docker ps` (should show only 4 containers)
