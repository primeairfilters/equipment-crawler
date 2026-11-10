-- P14 (D43, D44): the search Breeze runs, measured by ./crawl ask and ./crawl eval. Breeze's PHP tool copies this file.
-- Three ways inside one narrowed set of live sections, merged by reciprocal rank (k = 60):
--   meaning: cosine distance to the question's vector (text-embedding-3-small, 1536), closer than %(max_distance)s
--   words:   full text, any of the question's words, each weighed by its rarity (IDF), double in the heading path
--            (tsv_weighted, migration 0004) and once more in the manual's title (title_tsv, migration 0005); a natural
--            question rarely holds every word, and common words are noise
--   codes:   section_terms, exact codes from the question (E4, LOPS, part and model numbers), most codes matched first
-- Parameters: question text, question vector ('[..]'), codes (text[]), documents (bigint[] or NULL), manufacturer
-- (integer or NULL), language (a section's own language, or NULL for any), depth (rows per way), top (rows
-- returned), max_distance, equipment (text[]: the equipment types the question names, from EQUIPMENT_WORDS in
-- crawler/search.py; empty for none) and boost (a section of one of those types has its merged score times this).
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
), lexemes AS MATERIALIZED (
  -- each word of the question, weighed by how rare it is among live sections, counting its manual's title (IDF):
  -- "thermistor" counts, "air" barely. Counted ahead in kb.word_rarity (migration 0036); '' is a word no section has
  SELECT l.query, COALESCE(r.idf, u.idf) AS idf
  FROM (SELECT lexeme, quote_literal(lexeme)::tsquery AS query
        FROM unnest(tsvector_to_array(to_tsvector('english', %(question)s))) AS lexeme) l
  JOIN kb.word_rarity u ON u.lexeme = ''
  LEFT JOIN kb.word_rarity r ON r.lexeme = l.lexeme COLLATE "C"
), word_hits AS (
  -- a word counts double in the heading path, once in the body, and half again in the manual's title (the product;
  -- full credit spread over every section of a manual and let a brochure titled "Mini-Split" beat the service manual).
  -- Each share comes straight from an index, never by unpacking a section's words: once for the word anywhere in
  -- the section (sections_live_tsv_weighted), once more in its heading (sections_live_heading_tsv, migration 0037:
  -- keep the expression exactly as the index has it) and half again in its manual's title. Unpacking every matched
  -- section for the heading was 9.4 of 10 s on prod (245k sections, 2026-11-10)
  SELECT s.id AS section_id, l.idf AS part FROM lexemes l JOIN kb.sections s ON s.tsv_weighted @@ l.query
  WHERE s.is_live
    AND (%(documents)s::bigint[] IS NULL OR s.document_id = ANY (%(documents)s::bigint[]))
    AND (%(manufacturer)s::integer IS NULL OR s.manufacturer_id = %(manufacturer)s::integer)
    AND (%(language)s::text IS NULL OR s.language = %(language)s::text)
  UNION ALL
  SELECT s.id, l.idf FROM lexemes l JOIN kb.sections s ON to_tsvector('english'::regconfig, s.heading_path) @@ l.query
  WHERE s.is_live
    AND (%(documents)s::bigint[] IS NULL OR s.document_id = ANY (%(documents)s::bigint[]))
    AND (%(manufacturer)s::integer IS NULL OR s.manufacturer_id = %(manufacturer)s::integer)
    AND (%(language)s::text IS NULL OR s.language = %(language)s::text)
  UNION ALL
  SELECT s.id, l.idf * 0.5 FROM lexemes l JOIN kb.documents d ON d.title_tsv @@ l.query
  JOIN kb.sections s ON s.document_id = d.id
  WHERE s.is_live
    AND (%(documents)s::bigint[] IS NULL OR s.document_id = ANY (%(documents)s::bigint[]))
    AND (%(manufacturer)s::integer IS NULL OR s.manufacturer_id = %(manufacturer)s::integer)
    AND (%(language)s::text IS NULL OR s.language = %(language)s::text)
), words AS (
  SELECT section_id, row_number() OVER (ORDER BY SUM(part) DESC, section_id) AS r
  FROM word_hits GROUP BY section_id HAVING SUM(part) > 0
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
       s.kind, s.body, d.content_sha,
       f.score * CASE WHEN s.equipment_type = ANY (%(equipment)s::text[]) THEN %(boost)s ELSE 1 END AS score,
       f.meaning_rank, f.distance, f.words_rank, f.codes_rank
FROM fused f JOIN kb.sections s ON s.id = f.section_id JOIN kb.documents d ON d.id = s.document_id
ORDER BY score DESC, f.section_id
LIMIT %(top)s
