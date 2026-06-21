-- Diurnal traffic profile on a running loop campaign (Phase 2 shaper).
-- A profiled campaign's attempt rate is stepped hourly to follow the diurnal
-- curve (gencall.core.traffic_profile) so the traffic reads as organic. These
-- columns carry the campaign's profile so it survives a restart: profile_enabled
-- gates the shaper, the seven knobs are the make_curve kwargs (preset + curve
-- shape), and target_minutes (column from 0002) is the daily minutes target the
-- per-hour rate is sized from. Each ADD COLUMN is its own statement so the
-- migration runner applies them one at a time.
ALTER TABLE loop_campaigns ADD COLUMN profile_enabled BOOLEAN DEFAULT 0;
ALTER TABLE loop_campaigns ADD COLUMN profile_preset VARCHAR(32) DEFAULT 'diurnal';
ALTER TABLE loop_campaigns ADD COLUMN night_floor FLOAT DEFAULT 0.25;
ALTER TABLE loop_campaigns ADD COLUMN ramp_up_start INTEGER DEFAULT 6;
ALTER TABLE loop_campaigns ADD COLUMN plateau_start INTEGER DEFAULT 9;
ALTER TABLE loop_campaigns ADD COLUMN plateau_end INTEGER DEFAULT 18;
ALTER TABLE loop_campaigns ADD COLUMN ramp_down_end INTEGER DEFAULT 22;
ALTER TABLE loop_campaigns ADD COLUMN tz_offset INTEGER DEFAULT 0;
