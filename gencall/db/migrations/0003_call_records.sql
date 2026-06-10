-- Per-call records (design §4.2 / §5). The CallRecord tail-parser
-- (gencall/core/call_records.py) ingests SIPp <log> lines into this table, one
-- row per call per direction, timestamped at the RFC 3261 reference events:
--
--   A-side (out): duration_ms = t_bye_sent      - t_200ok_received
--   B-side (in):  duration_ms = t_bye_received  - t_200ok_sent
--
-- Milliseconds are stored RAW (t_*_ms and duration_ms); any rounding to whole
-- seconds is display-only. Failed calls store final_code (404/487/503/...) with
-- duration_ms = 0. call_uuid is the SIP Call-ID; (campaign_id, direction,
-- call_uuid) uniquely identifies a record so re-ingesting the same log lines is
-- idempotent. This is the growth table (§5) — a later retention job prunes it,
-- interval-gated, never per-iteration.
CREATE TABLE IF NOT EXISTS call_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         VARCHAR(255),               -- nullable: one-shot tests have no campaign
    direction           VARCHAR(4) NOT NULL,        -- out | in
    call_uuid           VARCHAR(255) NOT NULL,      -- SIP Call-ID
    a_number            VARCHAR(64),                -- calling (out) / from_number (in)
    b_number            VARCHAR(64),                -- called  (out) / to_number   (in)
    source_ip           VARCHAR(64),                -- inbound only: network peer (MADA whitelist tag)
    t_start_ms          BIGINT,                     -- out: t_invite        | in: t_invite_received
    t_answer_ms         BIGINT,                     -- out: t_200ok_received | in: t_200ok_sent
    t_end_ms            BIGINT,                     -- out: t_bye_sent       | in: t_bye_received
    duration_ms         BIGINT NOT NULL DEFAULT 0,  -- t_end_ms - t_answer_ms (0 for failed calls)
    final_code          INTEGER,                    -- final SIP response code (200/404/487/503/...)
    matched_record_id   INTEGER,                    -- LoopMatcher fills this (later stage)
    created_at          VARCHAR(64)
);

-- Idempotent ingest: one row per call per direction per campaign.
CREATE UNIQUE INDEX IF NOT EXISTS ix_call_records_uuid
    ON call_records (campaign_id, direction, call_uuid);

-- Matcher joins inbound to outbound on the number pair within a time window.
CREATE INDEX IF NOT EXISTS ix_call_records_match
    ON call_records (b_number, direction);
