-- P14 (D43, D44): the search Breeze runs, measured by ./crawl ask and ./crawl eval. Breeze's PHP tool copies this file.
-- Three ways inside one narrowed set of live sections, merged by reciprocal rank (k = 60):
--   meaning: cosine distance to the question's vector (text-embedding-3-small, 1536), closer than %(max_distance)s
--   words:   full text, any of the question's words, each weighed by its rarity (IDF) and double in the heading path
--            (tsv_weighted, migration 0004); a natural question rarely holds every word, and common words are noise
--   codes:   section_terms, exact codes from the question (E4, LOPS, part and model numbers), most codes matched first
-- Parameters: question text, question vector ('[..]'), codes (text[]), documents (bigint[] or NULL), manufacturer
-- (integer or NULL), language (a section's own language, or NULL for any), depth (rows per way), top (rows
-- returned), max_distance.
WITH meaning AS (
  SELECT v.section_id, v.embedding <=> %(vector)s::halfvec AS distance
  FROM kb.section_vectors_te3s_1536 v JOIN kb.sections s ON s.id = v.section_id
  WHERE v.is_live AND s.is_live
    AND (%(documents)s::bigint[] IS NULL OR s.document_id = ANY (%(documents)s::bigint[]))
    AND (%(manufacturer)s::integer IS NULL OR v.manufacturer_id = %(manufacturer)s::integer)
    AND (%(language)s::text IS NULL OR v.language = %(language)s::text)
  ORDER BY distance LIMIT %(depth)s
), meaning_ranked AS (
  SELECT section_id, distance, row_number() OVER (ORDER BY distance, section_id) AS r
  FROM meaning WHERE distance < %(max_distance)s
), lexemes AS (
  -- each word of the question, weighed by how rare it is among live sections (IDF): "thermistor" counts, "air" barely
  SELECT l.query, l.heading, ln((SELECT COUNT(*) FROM kb.sections WHERE is_live)::numeric
                                / GREATEST((SELECT COUNT(*) FROM kb.sections s WHERE s.is_live AND s.tsv_weighted @@ l.query), 1)) AS idf
  FROM (SELECT quote_literal(lexeme)::tsquery AS query, (quote_literal(lexeme) || ':A')::tsquery AS heading
        FROM unnest(tsvector_to_array(to_tsvector('english', %(question)s))) AS lexeme) l
), words AS (
  SELECT s.id AS section_id, row_number() OVER (ORDER BY x.score DESC, s.id) AS r
  FROM kb.sections s
  CROSS JOIN LATERAL (SELECT SUM(l.idf * CASE WHEN s.tsv_weighted @@ l.heading THEN 2 ELSE 1 END) AS score
                      FROM lexemes l WHERE s.tsv_weighted @@ l.query) x
  WHERE s.is_live AND x.score > 0
    AND (%(documents)s::bigint[] IS NULL OR s.document_id = ANY (%(documents)s::bigint[]))
    AND (%(manufacturer)s::integer IS NULL OR s.manufacturer_id = %(manufacturer)s::integer)
    AND (%(language)s::text IS NULL OR s.language = %(language)s::text)
  ORDER BY r LIMIT %(depth)s
), codes AS (
  SELECT section_id, row_number() OVER (ORDER BY matched DESC, section_id) AS r FROM (
    SELECT st.section_id, COUNT(*) AS matched
    FROM kb.section_terms st JOIN kb.sections s ON s.id = st.section_id
    WHERE st.term = ANY (%(codes)s::text[]) AND s.is_live
      AND (%(documents)s::bigint[] IS NULL OR s.document_id = ANY (%(documents)s::bigint[]))
      AND (%(manufacturer)s::integer IS NULL OR s.manufacturer_id = %(manufacturer)s::integer)
      AND (%(language)s::text IS NULL OR s.language = %(language)s::text)
    GROUP BY st.section_id) c
  ORDER BY r LIMIT %(depth)s
), fused AS (
  SELECT COALESCE(m.section_id, w.section_id, c.section_id) AS section_id,
         COALESCE(1.0 / (60 + m.r), 0) + COALESCE(1.0 / (60 + w.r), 0) + COALESCE(1.0 / (60 + c.r), 0) AS score,
         m.r AS meaning_rank, m.distance, w.r AS words_rank, c.r AS codes_rank
  FROM meaning_ranked m
  FULL JOIN words w ON w.section_id = m.section_id
  FULL JOIN codes c ON c.section_id = COALESCE(m.section_id, w.section_id)
)
SELECT f.section_id, s.document_id, d.title, s.doc_type, s.page_from, s.page_to, d.canonical_url, s.heading_path,
       s.kind, s.body, d.content_sha, f.score, f.meaning_rank, f.distance, f.words_rank, f.codes_rank
FROM fused f JOIN kb.sections s ON s.id = f.section_id JOIN kb.documents d ON d.id = s.document_id
ORDER BY f.score DESC, f.section_id
LIMIT %(top)s
