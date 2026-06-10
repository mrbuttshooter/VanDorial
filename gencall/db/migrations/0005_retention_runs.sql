-- Retention bookkeeping (design §5 retention, §7 stage 10).
--
-- The call_records table is the growth table (≈ 24k rows/day/direction at 50
-- concurrent loops). A retention job prunes rows older than a configurable
-- window (default 30 days). The single hard rule from the spec: the prune is
-- INTERVAL-GATED, never per-iteration — we must NOT recreate sigma's DELETE
-- storm (the old NetAxis build issued a DELETE on every scheduler tick).
--
-- This table records the last time a prune actually ran. The job reads it at
-- the top of every pass and SKIPS the DELETE unless at least the configured
-- interval has elapsed since last_run_at. Persisting the timestamp (rather than
-- keeping it in memory) means a crash/restart loop cannot bypass the gate and
-- DELETE on every boot — the gate survives restarts.
--
-- One row per logical job name (currently just 'call_records'); upserted by the
-- job. last_run_at is epoch seconds (REAL) for cheap arithmetic comparison.
CREATE TABLE IF NOT EXISTS retention_runs (
    job_name        VARCHAR(64) PRIMARY KEY,    -- logical retention job id
    last_run_at     REAL NOT NULL DEFAULT 0,    -- epoch seconds of last actual prune
    last_deleted    INTEGER NOT NULL DEFAULT 0, -- rows removed on the last prune
    updated_at      VARCHAR(64)                 -- ISO-8601 of the last gate update
);
