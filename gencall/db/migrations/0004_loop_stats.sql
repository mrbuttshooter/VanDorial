-- Loop stats (design §4.3 / §5). The LoopMatcher (gencall/core/loop_matcher.py)
-- runs a periodic (>= 10 s while a campaign runs) DB job that joins outbound and
-- inbound call_records on the number pair within a configurable window (default
-- 1 h) and writes one snapshot row per campaign per run here:
--
--   * calls_out / answered_out / minutes_out_ms  (A-side ms summed),
--   * calls_in_matched / minutes_in_ms           (B-side ms summed; see below),
--   * completion_pct                             (matched_in / answered_out),
--   * per-call delta (in_ms - out_ms) avg/p50/p95,
--   * failures_json                              (failures by SIP code, out & in
--                                                 separately + delta histogram +
--                                                 unmatched pairs).
--
-- minutes_in_ms counts ALL inbound minutes (matched + unmatched) so it is never
-- understated (design §4.3 — "unmatched inbound calls are still counted in
-- totals"). calls_in_matched is the matched count used for completion %.
--
-- This table is append-only (one row per matcher pass); GET /api/loops/{id}
-- returns the latest row (max ts). The matcher itself also stamps
-- call_records.matched_record_id as it pairs records, so a re-run is idempotent.
CREATE TABLE IF NOT EXISTS loop_stats (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         VARCHAR(255) NOT NULL,
    ts                  VARCHAR(64) NOT NULL,       -- ISO-8601, timezone-aware UTC
    calls_out           INTEGER NOT NULL DEFAULT 0, -- outbound records in window
    answered_out        INTEGER NOT NULL DEFAULT 0, -- outbound calls that answered (2xx)
    minutes_out_ms      BIGINT  NOT NULL DEFAULT 0, -- sum of A-side duration_ms
    calls_in_matched    INTEGER NOT NULL DEFAULT 0, -- inbound calls matched to an outbound
    minutes_in_ms       BIGINT  NOT NULL DEFAULT 0, -- sum of ALL inbound duration_ms (never understated)
    completion_pct      FLOAT   NOT NULL DEFAULT 0, -- matched_in / answered_out * 100
    delta_avg_ms        FLOAT   NOT NULL DEFAULT 0, -- avg (in_ms - out_ms) over matched pairs
    delta_p50_ms        FLOAT   NOT NULL DEFAULT 0, -- p50 of the per-call delta
    delta_p95_ms        FLOAT   NOT NULL DEFAULT 0, -- p95 of the per-call delta
    failures_json       TEXT                        -- JSON: failures_out, failures_in, histogram, unmatched_pairs
);

-- GET /api/loops/{id} reads the latest snapshot for a campaign (max ts / id).
CREATE INDEX IF NOT EXISTS ix_loop_stats_campaign
    ON loop_stats (campaign_id, id);
