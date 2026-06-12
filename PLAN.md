# R20: Reduce glm5.1 variants from 1000 to 10 per key group (41003)

## Why

Current 41003 has 7 key groups × 1000 variants = 7000 deployments. The original theory was that more variant IDs = more independent quota (200/id/day) = fewer 429s. **This theory is wrong**:

- ModelScope quota is **2000/key/day** total (not cumulative per variant). Each variant's 200/id/key quota shares the same per-key cap.
- 10 variants × 200/id/key = 2000/key/day = the per-key RPM cap. 1000 variants doesn't increase effective capacity beyond the 2000/key/day limit.
- The real bottleneck is **per-key token quota** (untracked, hourly/daily), which is entirely key-dependent — variant count is irrelevant.
- Key round-robin (R19) already distributes load evenly across keys. The 10 variants per key group provide enough RPM slots.
- **Massive resource savings**: 7000→70 deployments eliminates LiteLLM startup overhead, reduces memory pressure, allows lower nofile limits.

## Changes

### 1. `configs/litellm-glm51-test/config.yaml` — COMPLETE rewrite

- **From**: 7 key groups × 1000 variants = 7000 deployments (77059 lines)
- **To**: 7 key groups × 10 variants = 70 deployments (~950 lines)
- First 10 variant model IDs from current config (same permutation order):
  1. `ZHIPUAI/GLM-5.1` (v1 - original)
  2. `ZHIPUAI/GLm-5.1` (v2)
  3. `ZHIPUAI/GlM-5.1` (v3)
  4. `ZHIPUAI/Glm-5.1` (v4)
  5. `ZHIPUAI/gLM-5.1` (v5)
  6. `ZHIPUAI/gLm-5.1` (v6)
  7. `ZHIPUAI/glM-5.1` (v7)
  8. `ZHIPUAI/glm-5.1` (v8)
  9. `ZHIPUAi/GLM-5.1` (v9)
  10. `ZHIPUAi/GLm-5.1` (v10)
- Key assignment unchanged: k1=MS_KEY1, ..., k7=MS_KEY7
- router_settings, litellm_settings, general_settings unchanged

### 2. `configs/docker-compose.yml` — Adjust 41003 resources

With 70 deployments instead of 7000:
- `nofile soft`: 8192 → 2048
- `memory limits`: 2048M → 1024M
- `memory reservations`: 768M → 256M
- `cpus limit`: 2.0 → 1.0
- `cpus reservation`: 0.5 → 0.25
- Remove "TEMPORARY TEST" comment, replace with R20 lean config description
- Update comment about 1000→10 variants

### 3. `configs/DEPLOY_STATUS.md` — Update for R20

### 4. `CLAUDE.md` — Update architecture description (1000→10 variants for 41003)

### 5. `configs/proxy/proxy.py` docstring — Update "1000 variants" reference (comment only)

## What NOT to change

- **41001 (backup)** config stays at 1000 variants for now — will be updated later after 41003 proven stable
- **42001 (dsv4p)** config stays at 11 variants — already lean
- **Variant model IDs themselves** — immutable; we only reduce the count per key group
- **rpm=1** per deployment — immutable
- **Key group names** (glm5.1k1~k7) — unchanged
- **proxy.py functional code** — unchanged, only docstring/comment updates

## Deploy Protocol (REMOTE FIRST — opc2_uname)

Per deploy crash lesson (R19): **Deploy on remote (opc2_uname) first, verify ≥2 hours, then local (opc_uname)**.

**Deploy order for LiteLLM config changes**:
1. Copy new config to `/opt/cc-infra/litellm-glm51-test/config.yaml` on opc2_uname (SSH)
2. `docker restart glm5.1_test41003` on opc2_uname
3. Verify LiteLLM `/v1/models` shows key groups
4. Test with curl
5. Wait 2+ hours for stability
6. Then update local (opc_uname) — DANGER, may crash self