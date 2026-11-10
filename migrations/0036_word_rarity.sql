-- How rare each word is among live sections, for the search's words way (search.sql). Counted here once instead of
-- on every Breeze question: that count was ~450 of the ~750 ms a search took on 17k sections (2026-11-10).
-- idf is exactly what search.sql counted live: ln(live sections / live sections with the word in their text or their
-- manual's title, at least 1). The row with lexeme '' is the idf of a word no live section has.
-- refresh_word_rarity() in crawler/kb.py brings it up to date; the nightly run and every command that turns sections on or off
-- call it. The unique index lets the refresh run CONCURRENTLY, so Breeze searches never wait on it. Additive.
CREATE MATERIALIZED VIEW IF NOT EXISTS kb.word_rarity AS
WITH per_doc AS (
  SELECT document_id, COUNT(*) AS n FROM kb.sections WHERE is_live GROUP BY document_id
), title AS (
  SELECT p.document_id, p.n, unnest(tsvector_to_array(d.title_tsv)) AS lexeme
  FROM per_doc p JOIN kb.documents d ON d.id = p.document_id
), counts AS (
  -- a section counts once per word: by its manual's title, or else by its own text
  SELECT lexeme, SUM(n) AS n FROM (
    SELECT t.lexeme, t.n FROM title t
    UNION ALL
    SELECT b.lexeme, 1 FROM (SELECT s.document_id, unnest(tsvector_to_array(s.tsv_weighted)) AS lexeme
                             FROM kb.sections s WHERE s.is_live) b
    WHERE NOT EXISTS (SELECT 1 FROM title t WHERE t.document_id = b.document_id AND t.lexeme = b.lexeme)
  ) x GROUP BY lexeme
), total AS (
  SELECT COUNT(*)::numeric AS n FROM kb.sections WHERE is_live
)
SELECT c.lexeme COLLATE "C" AS lexeme, ln(total.n / GREATEST(c.n, 1)) AS idf FROM counts c, total
UNION ALL
SELECT '' COLLATE "C", ln(total.n / 1) FROM total WHERE total.n > 0
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS word_rarity_lexeme ON kb.word_rarity (lexeme);

GRANT SELECT ON kb.word_rarity TO eq_breeze, eq_runtime;
-- MAINTAIN (PostgreSQL 17+) lets the crawler's own login refresh it without owning it
GRANT MAINTAIN ON kb.word_rarity TO eq_runtime;
