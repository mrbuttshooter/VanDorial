-- Reliability layer (design §4.5 / §5): registry of every SIPp PID we spawn.
--
-- On startup the reconciler reads this table and kills any PID still alive whose
-- cmdline still hashes to what we recorded (guards against PID reuse). On clean
-- shutdown / stop the rows are cleared. cmdline_hash is a SHA-256 of the exact
-- argv we launched, so a recycled PID running something else is left untouched.
CREATE TABLE IF NOT EXISTS managed_processes (
    pid           INTEGER NOT NULL,
    role          VARCHAR(16) NOT NULL,      -- uac | uas (free-form; instance ids allowed)
    campaign_id   VARCHAR(255),              -- nullable: one-shot tests have no campaign
    cmdline_hash  VARCHAR(64) NOT NULL,      -- SHA-256 of the spawned argv
    spawned_at    VARCHAR(64) NOT NULL,      -- ISO-8601, timezone-aware UTC
    PRIMARY KEY (pid)
);
