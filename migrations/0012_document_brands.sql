-- A manual can belong to several brands (Q23, D82): Ruud and Rheem publish the same PDFs, and a file found again under
-- another brand is one document. Search prefers a brand's documents through this link, never excludes the rest.
-- Filled by the crawler whenever a brand's address serves a document; backfilled here with each document's own
-- manufacturer. Additive: a new table and rows.
CREATE TABLE IF NOT EXISTS kb.document_brands (
  document_id bigint NOT NULL REFERENCES kb.documents (id),
  manufacturer_id integer NOT NULL REFERENCES kb.manufacturers (id),
  first_seen_at timestamp without time zone NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
  PRIMARY KEY (document_id, manufacturer_id)
);
CREATE INDEX IF NOT EXISTS document_brands_manufacturer ON kb.document_brands (manufacturer_id);
INSERT INTO kb.document_brands (document_id, manufacturer_id)
SELECT id, manufacturer_id FROM kb.documents WHERE manufacturer_id IS NOT NULL
ON CONFLICT DO NOTHING;
