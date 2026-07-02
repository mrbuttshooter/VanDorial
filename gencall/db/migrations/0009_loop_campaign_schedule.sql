-- Optional daily active window on a loop campaign (schedule windows).
-- When schedule_enabled is set, the LoopEngine's shaper thread pauses the
-- campaign's dialer (stops the UAC) outside [schedule_start_min, schedule_end_min)
-- and resumes it inside — minutes since local midnight, local = schedule_tz_offset
-- hours from UTC. start == end means "always on". The window survives a restart
-- (the campaign row stays 'running'; auto-resume re-reads these columns and the
-- shaper re-applies the gate immediately). Each ADD COLUMN is its own statement
-- so the migration runner applies them one at a time.
ALTER TABLE loop_campaigns ADD COLUMN schedule_enabled BOOLEAN DEFAULT 0;
ALTER TABLE loop_campaigns ADD COLUMN schedule_start_min INTEGER DEFAULT 0;
ALTER TABLE loop_campaigns ADD COLUMN schedule_end_min INTEGER DEFAULT 0;
ALTER TABLE loop_campaigns ADD COLUMN schedule_tz_offset INTEGER DEFAULT 0;
