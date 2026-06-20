# TUNE_RULES.md — Parameter Auto-Tuning Rules (R35)
#
# This table defines safe automatic adjustments for auto_tune.sh.
# Each rule specifies: metric condition → parameter change → bounds.
# Changes outside bounds require manual/AI-agent confirmation.
#
# Format: | Metric Condition | Auto-Adjust | Bound | Rationale |

## MIN_OUTBOUND_INTERVAL_S (R31.9 burst throttle)

| Condition | Current → New | Operation | Bound | Rationale |
|-----------|--------------|-----------|-------|-----------|
| 429_rate > 30% | +0.5s | increase | ≤5.0s | Burst traffic overwhelming ModelScope RPM token-bucket |
| 429_rate > 50% | +1.0s | increase | ≤5.0s | Severe burst — longer cooldown |
| 429_rate < 5% AND NV_ratio < 20% | -0.3s | decrease | ≥0.5s | Low 429 means throttle too conservative, can speed up |
| 429_rate < 2% for 4+ hours | -0.5s | decrease | ≥0.5s | Sustained low 429 — safe to be more aggressive |

## UPSTREAM_TIMEOUT (per-key HTTP timeout)

| Condition | Current → New | Operation | Bound | Rationale |
|-----------|--------------|-----------|-------|-----------|
| timeout_rate > 10% | +15s | increase | ≤120s | Network instability or slow upstream |
| timeout_rate > 30% | +30s | increase | ≤120s | Severe upstream slowness |
| timeout_rate < 1% AND avg_duration < 10s | -10s | decrease | ≥30s | Upstream fast — reduce blocking time on rare failures |

## PROXY_TIMEOUT (overall request timeout)

| Condition | Current → New | Operation | Bound | Rationale |
|-----------|--------------|-----------|-------|-----------|
| Many MS cycles (>3 avg per req) | +30s | increase | ≤600s | More cycling time needed |
| Few cycles (<0.5 avg) AND avg_duration < 30s | -30s | decrease | ≥120s | Requests complete fast, shorter timeout safe |

## NV Interleaving (40005 only)

| Condition | Current → New | Operation | Bound | Rationale |
|-----------|--------------|-----------|-------|-----------|
| NV TTFB_avg > 8s | Log only | no change | — | NV upstream issue, needs human investigation |
| NV 429_rate > 10% | NV_NUM_KEYS→reduce to 3 | decrease | ≥1 | Bad NV keys, reduce waste |
| Health score: NV bonus helps (score05 > score01 + 5) | — | observe | — | NV interleaving is beneficial, keep it |
| Health score: NV hurts (score05 < score01 - 10) | NV_NUM_KEYS→0 | disable | — | NV making things worse, disable for 40005 |

## Safety Constraints (NEVER auto-adjust)

| Parameter | Reason |
|-----------|--------|
| NUM_KEYS | Tied to physical MS API keys |
| NUM_VARIANTS_GLM51 / NUM_VARIANTS_DSV4P | Each variant has independent quota |
| MODEL_INPUT_TOKEN_SAFETY_* | Tied to context window, affects CC behavior |
| NV_BASEURL / NV_KEY* | Fixed API credentials |
| NV_PROXY_URL | Tied to mihomo config |
| LITELLM_URL_* | Tied to LiteLLM container address |

## Version Promotion Rules

| Condition | Action |
|-----------|--------|
| score05 >= score01 + 5 for 2+ consecutive hours | PROMOTE: sync 40005 params → 40001 |
| score05 < score01 - 10 for 1+ hour | ROLLBACK: restore 40005 to 40001 params |
| 40005 crash/restart loop detected | EMERGENCY: set 40005 NV_NUM_KEYS=0, revert to 40001 params |
| 40001 crash/restart loop detected | ALERT: both proxies unstable, manual intervention needed |
