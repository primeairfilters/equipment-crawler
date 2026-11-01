-- Brands take turns in the nightly window (P24, D91, user 2026-10-31: 2 to 6 AM). last_turn_run is the run that last
-- gave the brand its turn; the next run starts with the oldest, so a brand the window did not reach goes first the next
-- night. Additive: one nullable column.
ALTER TABLE crawl.brands ADD COLUMN IF NOT EXISTS last_turn_run bigint NULL;
