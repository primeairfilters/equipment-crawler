-- P13 (D42, D52): section vectors, one table per embedding model and size. Additive only: safe to run twice.
-- text-embedding-3-small at 1536 dimensions (Breeze's model and size), stored as halfvec. Copied is_live and filters
-- (D48) are kept true by a trigger on kb.sections, so publish, hide, unhide and a retag can never leave them stale.
-- prune removes a stale section's vector itself, before the section (no cascading keys, house rule).
-- Statements are split on a semicolon at the end of a line, so the function and trigger bodies never end a line
-- with one; they are created only when missing, so a second run changes nothing.

CREATE TABLE IF NOT EXISTS kb.section_vectors_te3s_1536 (
  section_id bigint PRIMARY KEY REFERENCES kb.sections (id),
  embed_sha char(64) COLLATE "C" NOT NULL,  -- sha256 of the exact text embedded: the same text reuses its vector
  embedding halfvec(1536) NOT NULL,
  is_live boolean NOT NULL DEFAULT false,
  manufacturer_id integer NULL,
  trade text NULL,
  equipment_type text NULL,
  doc_type text NULL,
  language text NULL,
  created_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);
CREATE INDEX IF NOT EXISTS section_vectors_te3s_1536_sha ON kb.section_vectors_te3s_1536 (embed_sha);
CREATE INDEX IF NOT EXISTS section_vectors_te3s_1536_filter ON kb.section_vectors_te3s_1536 (manufacturer_id, equipment_type)
  WHERE is_live;
CREATE INDEX IF NOT EXISTS section_vectors_te3s_1536_hnsw ON kb.section_vectors_te3s_1536
  USING hnsw (embedding halfvec_cosine_ops) WHERE is_live;

DO $do$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'sync_section_vectors' AND pronamespace = 'kb'::regnamespace)
  THEN EXECUTE $f$CREATE FUNCTION kb.sync_section_vectors() RETURNS trigger LANGUAGE plpgsql AS $b$
    BEGIN UPDATE kb.section_vectors_te3s_1536 SET is_live = NEW.is_live, manufacturer_id = NEW.manufacturer_id,
      trade = NEW.trade, equipment_type = NEW.equipment_type, doc_type = NEW.doc_type, language = NEW.language
      WHERE section_id = NEW.id; RETURN NULL; END
    $b$$f$; END IF; END
$do$;

DO $do$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'sections_sync_vectors' AND tgrelid = 'kb.sections'::regclass)
  THEN EXECUTE $t$CREATE TRIGGER sections_sync_vectors AFTER UPDATE OF is_live, manufacturer_id, trade, equipment_type,
    doc_type, language ON kb.sections FOR EACH ROW
    WHEN ((OLD.is_live, OLD.manufacturer_id, OLD.trade, OLD.equipment_type, OLD.doc_type, OLD.language)
          IS DISTINCT FROM (NEW.is_live, NEW.manufacturer_id, NEW.trade, NEW.equipment_type, NEW.doc_type, NEW.language))
    EXECUTE FUNCTION kb.sync_section_vectors()$t$; END IF; END
$do$;

GRANT SELECT, INSERT, UPDATE ON kb.section_vectors_te3s_1536 TO eq_runtime;
GRANT SELECT ON kb.section_vectors_te3s_1536 TO eq_breeze;
