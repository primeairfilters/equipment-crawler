-- The admin page's settings (P25, D93, user 2026-10-31: "a new tab ... called Settings ... the start and end times
-- ... so I can change them"). One row: the nightly window's start and stop, whole hours in Pacific time (default 2 and
-- 6). `./crawl auto --nightly`, run by the server's cron every hour, starts only in the start hour and starts no new
-- work from the stop hour (the next day when it is earlier than the start). Additive: a table with its one row.
CREATE TABLE IF NOT EXISTS control.settings (
  id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  window_start_hour integer NOT NULL DEFAULT 2 CHECK (window_start_hour BETWEEN 0 AND 23),
  window_stop_hour integer NOT NULL DEFAULT 6 CHECK (window_stop_hour BETWEEN 0 AND 23),
  updated_at timestamp without time zone NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
  CHECK (window_start_hour <> window_stop_hour)
);
INSERT INTO control.settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

GRANT SELECT ON control.settings TO eq_runtime, eq_admin;
GRANT UPDATE (window_start_hour, window_stop_hour, updated_at) ON control.settings TO eq_admin;
