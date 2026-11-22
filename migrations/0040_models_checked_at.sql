-- When a manual with no model numbers was last searched for its brand's known model numbers (crawler/known_models.py,
-- 2026-11-21), so it is read again only after 30 days, not every night. Additive: one column.
ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS models_checked_at timestamp without time zone NULL;
