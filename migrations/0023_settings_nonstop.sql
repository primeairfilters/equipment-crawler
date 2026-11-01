-- The Settings tab's "Run all the time" switch (user 2026-11-01: "a switch that if I turn it on the crawler will just
-- run and ignore the times. If I turn it off then it will go back to the times"). On: `./crawl auto --nightly` starts
-- a run whenever none is going and none started in the last hour, with no stop; a going run re-reads it each minute.
-- Off by default. Additive: one column.
ALTER TABLE control.settings ADD COLUMN IF NOT EXISTS run_nonstop boolean NOT NULL DEFAULT false;

GRANT UPDATE (run_nonstop) ON control.settings TO eq_admin;
