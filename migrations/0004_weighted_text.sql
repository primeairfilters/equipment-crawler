-- P14 tuning: full text with the heading path weighted above the body (A over D), so a word in a section's heading
-- counts more than the same word once in a long body (the thermistor chart, under "Thermistor Resistance Values",
-- ranked #229 on words). Additive: a new generated column and its index next to tsv, which stays.
ALTER TABLE kb.sections ADD COLUMN IF NOT EXISTS tsv_weighted tsvector GENERATED ALWAYS AS (
  setweight(to_tsvector('english'::regconfig, heading_path), 'A') || setweight(to_tsvector('english'::regconfig, body), 'D')
) STORED;
CREATE INDEX IF NOT EXISTS sections_live_tsv_weighted ON kb.sections USING gin (tsv_weighted) WHERE is_live;
