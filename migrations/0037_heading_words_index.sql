-- The words of each live section's heading path, indexed on their own, for the search's words way (search.sql). A
-- heading word counts double, and checking that through tsv_weighted (its weight A part, migration 0004) unpacked
-- every matched section: 9.4 of 10 s for one question on prod (245k sections, 2026-11-10). This index answers it
-- straight away. The expression must stay exactly the one search.sql uses, or the index is never used. Additive.
CREATE INDEX IF NOT EXISTS sections_live_heading_tsv ON kb.sections
  USING gin (to_tsvector('english'::regconfig, heading_path)) WHERE is_live;
