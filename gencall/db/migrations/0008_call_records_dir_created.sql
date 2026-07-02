-- Index the matcher's ACTUAL inbound window scan (design §4.3).
--
-- The LoopMatcher's inbound pass runs every >= 10 s while any campaign is
-- running and reads:
--
--   WHERE direction = 'in' AND matched_record_id IS NULL
--     AND created_at >= :floor AND created_at <= :ceil
--   ORDER BY id DESC LIMIT :lim
--
-- 0003 indexed (direction, t_start_ms) for an earlier version of that scan;
-- the matcher has since moved its window predicate to created_at (the
-- cross-process-comparable wall clock), so that index no longer serves any
-- query while still taxing every insert on the growth table. Replace it with
-- the composite that matches the current predicate.
CREATE INDEX IF NOT EXISTS ix_call_records_dir_created
    ON call_records (direction, created_at);

DROP INDEX IF EXISTS ix_call_records_dir_start;
