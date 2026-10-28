-- P11 gathering (D59, D62). Additive, except one constraint swap the user approved on 2026-10-27:
-- pages.fetch_state gains skipped_rule (rules said no without a keyword) and skipped_user (`./crawl verdict ... skip`).
-- Swapping a CHECK constraint removes no data. The file runs in one transaction, so the swap is all or nothing.

ALTER TABLE crawl.pages DROP CONSTRAINT IF EXISTS pages_fetch_state_check;
ALTER TABLE crawl.pages ADD CONSTRAINT pages_fetch_state_check CHECK (fetch_state IN (
  'queued', 'fetched', 'failed', 'refused', 'beyond_depth', 'over_limit', 'skipped_keyword', 'skipped_rule',
  'skipped_ai', 'skipped_user', 'ask_ai', 'out_of_scope'));

-- Skip keywords the AI suggested. Shown in `status`; only the user turns one into a real keyword (D62).
CREATE TABLE IF NOT EXISTS crawl.keyword_suggestions (
  brand_id integer NOT NULL REFERENCES crawl.brands (id),
  keyword text COLLATE "C" NOT NULL,
  times integer NOT NULL DEFAULT 1,
  first_seen_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
  last_seen_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
  PRIMARY KEY (brand_id, keyword)
);

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA crawl TO eq_runtime;
