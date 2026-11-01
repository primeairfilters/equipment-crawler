-- The Settings tab's times go down to the minute (user 2026-11-01: "support down to the minute"). Two minute columns
-- beside the hours (0 by default, so 2 and 6 stay 2:00 and 6:00), and the start-not-the-stop check swapped for one
-- that compares hour and minute, so 3:00 to 3:30 is a window. `./crawl auto --nightly` is now called every minute
-- and starts once a night, from the start time until the stop time. Additive: two columns and a CHECK swapped
-- (DROP CONSTRAINT IF EXISTS removes no data; approved for 0002).
ALTER TABLE control.settings ADD COLUMN IF NOT EXISTS window_start_minute integer NOT NULL DEFAULT 0
  CHECK (window_start_minute BETWEEN 0 AND 59);
ALTER TABLE control.settings ADD COLUMN IF NOT EXISTS window_stop_minute integer NOT NULL DEFAULT 0
  CHECK (window_stop_minute BETWEEN 0 AND 59);
ALTER TABLE control.settings DROP CONSTRAINT IF EXISTS settings_check;
ALTER TABLE control.settings ADD CONSTRAINT settings_check
  CHECK ((window_start_hour, window_start_minute) <> (window_stop_hour, window_stop_minute));

GRANT UPDATE (window_start_minute, window_stop_minute) ON control.settings TO eq_admin;
