-- Loop campaigns (design §5). The full LoopEngine/API lands in a later stage;
-- this migration creates just enough of the table for the reliability layer to
-- mark a campaign 'interrupted' during startup reconciliation (§4.5). Later
-- stages extend the campaign lifecycle (start/stop/stats) on top of this.
CREATE TABLE IF NOT EXISTS loop_campaigns (
    id              VARCHAR(255) PRIMARY KEY,
    name            VARCHAR(255),
    status          VARCHAR(20) NOT NULL DEFAULT 'stopped',  -- running|stopped|interrupted|completed
    node_id         VARCHAR(255),
    dest_host       VARCHAR(255),
    dest_port       INTEGER,
    transport       VARCHAR(10),
    csv_path        VARCHAR(1024),
    rate            FLOAT,
    max_concurrent  INTEGER,
    duration_mode   VARCHAR(10),                              -- fixed|range
    duration_s      INTEGER,
    duration_max_s  INTEGER,
    match_key       VARCHAR(20),                              -- exact|suffix6|suffix8|...
    target_calls    INTEGER,
    target_minutes  INTEGER,
    created_at      VARCHAR(64),
    started_at      VARCHAR(64),
    stopped_at      VARCHAR(64)
);
