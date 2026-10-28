-- P14 tuning: the manual's title for word search. A question naming the product ("Floating Air mini split error
-- code 9") matched look-alike error-code tables of other product lines, since only the body and heading path were
-- searched. Additive: a generated column on kb.documents.
ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS title_tsv tsvector GENERATED ALWAYS AS (
  to_tsvector('english'::regconfig, COALESCE(title, ''))
) STORED;
