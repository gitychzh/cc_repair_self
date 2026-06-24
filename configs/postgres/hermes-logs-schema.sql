-- R40: Hermes hm40006 proxy request/tier-attempt logging schema.
-- Database: hermes_logs (created by POSTGRES_MULTIPLE_DATABASES or manually).
-- Run once: psql -U litellm -d hermes_logs -f hermes-logs-schema.sql
-- Idempotent: uses IF NOT EXISTS.

-- ─── hm_requests: one row per incoming /v1/chat/completions request ───
CREATE TABLE IF NOT EXISTS hm_requests (
    request_id              TEXT PRIMARY KEY,
    ts                      TIMESTAMPTZ NOT NULL,
    host_machine            TEXT NOT NULL,
    proxy_role              TEXT,
    request_model           TEXT,
    mapped_model            TEXT,
    agent_type              TEXT,
    stream                  BOOLEAN,
    total_input_chars       INTEGER DEFAULT 0,
    ttfb_ms                 INTEGER,
    duration_ms             INTEGER DEFAULT 0,
    status                  INTEGER DEFAULT 0,
    error_type              TEXT,
    error_message           TEXT,
    upstream_type           TEXT,
    tier_model              TEXT,
    nv_key_idx              INTEGER,
    litellm_model           TEXT,
    start_tier_idx          INTEGER,
    fallback_from           TEXT,
    fallback_to             TEXT,
    fallback_occurred       BOOLEAN DEFAULT FALSE,
    fallback_tiers_used     TEXT[],          -- ordered list of tiers in ring
    finish_reason           TEXT,
    input_tokens            INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    key_cycle_429s          INTEGER DEFAULT 0,
    key_cycle_details       JSONB,           -- full per-attempt detail (denormalized for convenience)
    error_subcategory       TEXT,            -- e.g. "all_tiers_failed", "tier_X_all_keys_failed"
    startup_retry           INTEGER,
    tiers_tried_count       INTEGER DEFAULT 0,
    fallback_actually_attempted BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for the common query patterns (by time, status, tier, error, model)
CREATE INDEX IF NOT EXISTS idx_hm_req_ts          ON hm_requests (ts DESC);
CREATE INDEX IF NOT EXISTS idx_hm_req_status      ON hm_requests (status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hm_req_tier        ON hm_requests (tier_model, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hm_req_error       ON hm_requests (error_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hm_req_mapped      ON hm_requests (mapped_model, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hm_req_host        ON hm_requests (host_machine, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hm_req_fallback    ON hm_requests (fallback_occurred, ts DESC) WHERE fallback_occurred;

-- ─── hm_tier_attempts: one row per per-key attempt within a tier ───
-- (exploded from key_cycle_details for per-key/per-error aggregation)
CREATE TABLE IF NOT EXISTS hm_tier_attempts (
    id              BIGSERIAL PRIMARY KEY,
    request_id      TEXT NOT NULL,
    tier            TEXT NOT NULL,
    nv_key_idx      INTEGER,
    litellm_model   TEXT,
    error_type      TEXT,
    elapsed_ms      INTEGER,
    upstream_type   TEXT,
    ts              TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hm_att_req      ON hm_tier_attempts (request_id);
CREATE INDEX IF NOT EXISTS idx_hm_att_tier_ts  ON hm_tier_attempts (tier, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hm_att_err_ts   ON hm_tier_attempts (error_type, ts DESC) WHERE error_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hm_att_key_ts   ON hm_tier_attempts (nv_key_idx, ts DESC);

-- ─── Retention: function + 30-day cleanup (called by cron / hm_log_cleanup.sh) ───
CREATE OR REPLACE FUNCTION hm_cleanup_old(p_days INTEGER DEFAULT 30) RETURNS INTEGER
LANGUAGE plpgsql AS $$
DECLARE
    deleted INTEGER;
BEGIN
    DELETE FROM hm_tier_attempts WHERE created_at < NOW() - (p_days || ' days')::INTERVAL;
    GET DIAGNOSTICS deleted = ROW_COUNT;
    DELETE FROM hm_requests WHERE ts < NOW() - (p_days || ' days')::INTERVAL;
    RETURN deleted;
END;
$$;

-- Helpful view: per-tier success rate over last hour
CREATE OR REPLACE VIEW v_hm_tier_health_1h AS
SELECT
    tier_model,
    COUNT(*) FILTER (WHERE status = 200) AS ok_1h,
    COUNT(*) FILTER (WHERE status >= 400) AS fail_1h,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status = 200) / NULLIF(COUNT(*), 0), 1) AS success_pct_1h,
    ROUND(AVG(duration_ms) FILTER (WHERE status = 200), 0) AS avg_duration_ms_1h
FROM hm_requests
WHERE ts > NOW() - INTERVAL '1 hour'
GROUP BY tier_model
ORDER BY tier_model;

-- Helpful view: per-key attempt error distribution (last 24h)
CREATE OR REPLACE VIEW v_hm_key_errors_24h AS
SELECT
    tier,
    nv_key_idx,
    error_type,
    COUNT(*) AS n,
    ROUND(AVG(elapsed_ms)) AS avg_elapsed_ms
FROM hm_tier_attempts
WHERE ts > NOW() - INTERVAL '24 hours'
GROUP BY tier, nv_key_idx, error_type
ORDER BY tier, nv_key_idx, n DESC;
