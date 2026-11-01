-- One trades list (user 2026-11-01: "whatever Tracforce uses to list those trades have it pull from there so if we add
-- more later then it will change in all areas"). The list is the main app's (GET /api/public/trades); the admin page
-- and `./crawl add-brands` check a brand's trades against it. So this CHECK no longer names the nine trades, which
-- would refuse a new one; it keeps only their shape, plain lowercase words. Additive: a CHECK swapped (DROP
-- CONSTRAINT IF EXISTS removes no data; approved for 0002).
ALTER TABLE control.brands DROP CONSTRAINT IF EXISTS brands_trades_check;
ALTER TABLE control.brands ADD CONSTRAINT brands_trades_check
  CHECK (array_to_string(trades, ',') ~ '^([a-z_]+(,[a-z_]+)*)?$');
