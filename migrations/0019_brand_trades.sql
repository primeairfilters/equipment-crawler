-- A brand's trades (P23, D87–D90, user 2026-10-31): the admin page picks one or more of Tracforce's trades for a
-- brand ("Goodman" is HVAC; Bosch is HVAC and Appliance Repair). An empty list means any trade, as before: the brands
-- added before this (Ruud) have none until edited. The crawler copies them to crawl.brands at the start of every run
-- (control.sync). Additive: two columns with a default and the page's two column grants. The page reads a brand's
-- trades from control.brands (it may already SELECT it); brand_status is not changed.
ALTER TABLE control.brands ADD COLUMN IF NOT EXISTS trades text[] NOT NULL DEFAULT '{}'
  CHECK (trades <@ ARRAY['hvac', 'plumbing', 'electrical', 'commercial_refrigeration', 'appliance_repair', 'pool_spa',
                         'generators', 'water_treatment', 'garage_doors']::text[]);
ALTER TABLE crawl.brands ADD COLUMN IF NOT EXISTS trades text[] NOT NULL DEFAULT '{}';

GRANT INSERT (trades), UPDATE (trades) ON control.brands TO eq_admin;
