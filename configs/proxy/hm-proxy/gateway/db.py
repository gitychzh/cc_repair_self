#!/usr/bin/env python3
"""PostgreSQL persistence for hm-proxy metrics — R40.

Engineering-grade long-term logging: every request (success or failure) and its
per-tier key-cycle attempts are written to the `hermes_logs` database for
structured querying, trend analysis, and post-mortem root-cause.

Design:
- Asynchronous: a single background daemon thread drains a queue.Queue and
  batch-INSERTs every FLUSH_INTERVAL_S or FLUSH_BATCH rows, whichever first.
  This never blocks the request path — if DB is down, the queue fills and
  entries are dropped (file logs remain the ground truth; DB is a convenience).
- Two tables: hm_requests (1 row per request) + hm_tier_attempts (1 row per
  per-key attempt within a tier, FK to request). key_cycle_details JSONB is
  also stored on the request row for convenience.
- Connection is lazily established and re-established on failure. A health
  ping every N flushes re-opens a dead connection.
- Disabled by default via HM_DB_ENABLED env (opt-in per machine).

Tables are created by configs/postgres/hermes-logs-schema.sql, run once at
DB initialization or manually via psql -f.
"""
import json
import os
import queue
import threading
import time
import datetime

try:
    import psycopg2
    from psycopg2.extras import execute_values  # batch INSERT
    _HAS_PSYCOPG = True
except ImportError:
    psycopg2 = None
    execute_values = None
    _HAS_PSYCOPG = False

# ─── Configuration (env-driven, per-machine) ──────────────────────────────
DB_ENABLED = os.environ.get("HM_DB_ENABLED", "0") == "1"
DB_HOST = os.environ.get("HM_DB_HOST", "cc_postgres")
DB_PORT = int(os.environ.get("HM_DB_PORT", "5432"))
DB_USER = os.environ.get("HM_DB_USER", "litellm")
DB_PASSWORD = os.environ.get("HM_DB_PASSWORD", "")
DB_NAME = os.environ.get("HM_DB_NAME", "hermes_logs")

FLUSH_INTERVAL_S = float(os.environ.get("HM_DB_FLUSH_INTERVAL_S", "2"))
FLUSH_BATCH = int(os.environ.get("HM_DB_FLUSH_BATCH", "50"))
QUEUE_MAX = int(os.environ.get("HM_DB_QUEUE_MAX", "2000"))  # drop beyond this

HOST_MACHINE = os.environ.get("HM_HOST_MACHINE") or os.environ.get("HOSTNAME") or "unknown"

# ─── Queue + worker ───────────────────────────────────────────────────────
_queue = queue.Queue(maxsize=QUEUE_MAX)
_worker_thread = None
_worker_stop = threading.Event()
_conn = None
_conn_lock = threading.Lock()
_last_health_check = 0.0


class _Disable:
    """Sentinel: DB disabled, all calls are no-ops."""
    pass


def _get_conn():
    """Lazily establish / re-establish DB connection. Returns conn or None."""
    global _conn
    if not _HAS_PSYCOPG or not DB_ENABLED:
        return None
    with _conn_lock:
        if _conn is not None:
            # Cheap liveness check
            try:
                with _conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return _conn
            except Exception:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None
        try:
            _conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=DB_NAME,
                connect_timeout=5,
            )
            _conn.autocommit = False
            return _conn
        except Exception as e:
            # Silent — DB is best-effort. Print once per ~60s to avoid log spam.
            now = time.time()
            global _last_health_check
            if now - _last_health_check > 60:
                print(f"[HM-DB] connect failed: {e}", flush=True)
                _last_health_check = now
            _conn = None
            return None


def _worker_loop():
    """Background thread: drain queue and batch-insert."""
    while not _worker_stop.is_set():
        try:
            batch = [_queue.get(timeout=FLUSH_INTERVAL_S)]
        except queue.Empty:
            continue
        # Drain any additional queued items (up to FLUSH_BATCH)
        while len(batch) < FLUSH_BATCH:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        _flush_batch(batch)


def _flush_batch(batch):
    """INSERT a batch of (metrics_dict) rows. On failure, drop silently."""
    if not batch:
        return
    conn = _get_conn()
    if conn is None:
        return  # DB unavailable; file logs remain ground truth
    try:
        with conn.cursor() as cur:
            # 1) Insert request rows, collect request_id → generated id mapping
            req_rows = []
            for m in batch:
                req_rows.append(_build_request_row(m))
            # execute_values returns the inserted ids in order
            request_ids = execute_values(
                cur,
                """INSERT INTO hm_requests
                   (request_id, ts, host_machine, proxy_role, request_model, mapped_model,
                    agent_type, stream, total_input_chars, ttfb_ms, duration_ms, status,
                    error_type, error_message, upstream_type, tier_model, nv_key_idx,
                    litellm_model, start_tier_idx, fallback_from, fallback_to,
                    fallback_occurred, fallback_tiers_used, finish_reason,
                    input_tokens, output_tokens, key_cycle_429s, key_cycle_details,
                    error_subcategory, startup_retry, tiers_tried_count,
                    fallback_actually_attempted)
                   VALUES %s
                   ON CONFLICT (request_id) DO UPDATE SET
                     status=EXCLUDED.status, duration_ms=EXCLUDED.duration_ms,
                     error_type=EXCLUDED.error_type, tier_model=EXCLUDED.tier_model
                   RETURNING request_id""",
                req_rows,
                page_size=100,
                fetch=True,
            )
            # 2) Insert tier_attempts rows for each request's key_cycle_details
            attempt_rows = []
            for m in batch:
                rid = m.get("request_id")
                for a in (m.get("key_cycle_details") or []):
                    attempt_rows.append((
                        rid,
                        a.get("tier"),
                        a.get("nv_key_idx"),
                        a.get("litellm_model"),
                        a.get("error_type"),
                        a.get("elapsed_ms"),
                        a.get("upstream_type"),
                        m.get("timestamp"),
                    ))
            if attempt_rows:
                execute_values(
                    cur,
                    """INSERT INTO hm_tier_attempts
                       (request_id, tier, nv_key_idx, litellm_model,
                        error_type, elapsed_ms, upstream_type, ts)
                       VALUES %s
                       ON CONFLICT DO NOTHING""",
                    attempt_rows,
                    page_size=200,
                )
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # Silent best-effort; print throttled
        now = time.time()
        global _last_health_check
        if now - _last_health_check > 60:
            print(f"[HM-DB] flush failed ({len(batch)} rows dropped): {e}", flush=True)
            _last_health_check = now


def _build_request_row(m):
    """Build a tuple matching the INSERT column order."""
    tiers_tried = m.get("fallback_tiers_used") or []
    return (
        m.get("request_id"),
        m.get("timestamp"),
        HOST_MACHINE,
        m.get("proxy_role"),
        m.get("request_model"),
        m.get("mapped_model"),
        m.get("agent_type"),
        m.get("stream"),
        m.get("total_input_chars", 0),
        m.get("ttfb_ms"),
        m.get("duration_ms", 0),
        m.get("status", 0),
        m.get("error_type"),
        m.get("error_message"),
        m.get("upstream_type"),
        m.get("tier_model"),
        m.get("nv_key_idx"),
        m.get("litellm_model"),
        m.get("start_tier_idx"),
        m.get("fallback_from"),
        m.get("fallback_to"),
        m.get("fallback_occurred", False),
        tiers_tried,
        m.get("finish_reason"),
        m.get("input_tokens", 0),
        m.get("output_tokens", 0),
        m.get("key_cycle_429s_before_success", 0),
        json.dumps(m.get("key_cycle_details") or [], ensure_ascii=False, default=str),
        m.get("error_subcategory"),
        m.get("startup_retry"),
        len(tiers_tried),
        len([t for t in (m.get("key_cycle_details") or []) if t.get("tier")]) > 1,
    )


def enqueue_metrics(metrics):
    """Enqueue a metrics dict for async DB write. Non-blocking, best-effort."""
    if not DB_ENABLED or not _HAS_PSYCOPG:
        return
    try:
        _queue.put_nowait(dict(metrics))  # shallow copy
    except queue.Full:
        # Queue full (DB backed up) — drop oldest by not enqueuing. File log is ground truth.
        pass


def start_worker():
    """Start the background DB writer thread. Call once at import."""
    global _worker_thread
    if not DB_ENABLED or not _HAS_PSYCOPG:
        return
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="hm-db-writer", daemon=True)
    _worker_thread.start()


def stop_worker():
    """Flush remaining queue and stop worker. Called on shutdown."""
    if _worker_thread is None:
        return
    _worker_stop.set()
    # Best-effort final flush
    try:
        batch = []
        while len(batch) < FLUSH_BATCH:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            _flush_batch(batch)
    except Exception:
        pass


# Start worker on import (if enabled)
start_worker()

import atexit
atexit.register(stop_worker)
