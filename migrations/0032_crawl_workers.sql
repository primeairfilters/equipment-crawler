-- Five cards across the top of the admin page, one per crawl worker, each with the brand it is crawling (user,
-- 2026-11-06, when the nightly run went to five brands at once, crawler/auto.py CRAWL_WORKERS). One row per worker,
-- written over as it takes a brand and set to no brand when it is done; the admin page shows every card idle when no
-- run is going, so a run that crashed never leaves a card stuck. Additive: a new table and its grants.
CREATE TABLE IF NOT EXISTS control.crawl_workers (
  worker integer PRIMARY KEY CHECK (worker > 0),
  brand text NULL,
  since timestamp without time zone NULL
);

GRANT SELECT, INSERT, UPDATE ON control.crawl_workers TO eq_runtime;
GRANT SELECT ON control.crawl_workers TO eq_admin;
