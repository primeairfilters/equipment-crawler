-- Form-number revisions: "M44-2230 Rev. 1" is M44-2230 at revision 1. The form rule (D55) groups a manufacturer's
-- versions of one form by its base and keeps the highest revision live, whichever was found first. Only an explicit
-- "Rev" counts: a trailing "-4" can be another variant (AP11828-4 Canada, AP11828-3 US). A date in brackets is ignored.
-- Additive: two functions and two generated columns, filled for every existing row.
CREATE FUNCTION kb.form_base(form text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT NULLIF(btrim(regexp_replace(regexp_replace(upper(form), '\s*\([^)]*\)', '', 'g'),
                                     '[\s,;]*\mREV(ISION)?\M\.?\s*[0-9A-Z]{1,3}\s*$', '')), '')
$$;
CREATE FUNCTION kb.form_rev(form text) RETURNS integer LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN tok IS NULL THEN 0 WHEN tok ~ '^[0-9]+$' THEN tok::integer
              WHEN tok ~ '^[A-Z]$' THEN ascii(tok) - 64 ELSE 0 END
  FROM (SELECT substring(regexp_replace(upper(form), '\s*\([^)]*\)', '', 'g')
                         FROM '\mREV(?:ISION)?\M\.?\s*([0-9A-Z]{1,3})\s*$') AS tok) t
$$;
ALTER TABLE kb.documents
  ADD COLUMN IF NOT EXISTS form_base text GENERATED ALWAYS AS (kb.form_base(form_number)) STORED,
  ADD COLUMN IF NOT EXISTS form_rev integer GENERATED ALWAYS AS (kb.form_rev(form_number)) STORED;
CREATE INDEX IF NOT EXISTS documents_form_base ON kb.documents (manufacturer_id, form_base) WHERE form_base IS NOT NULL;
