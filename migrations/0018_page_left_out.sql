-- Pages the reader leaves out on purpose (2026-10-30): an English manual followed by its Spanish and French copies
-- printed as outlines reads, after OCR, as English text then empty pages. left_out = 'language' says the page was
-- read and left out as Spanish or French (English only, 2026-10-28), so the parse-issue scan does not count it as
-- a page with no text (mostly_scanned; docs 69 and 70). Additive: a nullable column.
ALTER TABLE crawl.page_text ADD COLUMN IF NOT EXISTS left_out text NULL CHECK (left_out IN ('language'));
