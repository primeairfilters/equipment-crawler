-- EQUIPMENT_FILE_UPLOADS.md phase 2: a document carries its file type, so the library, reread, section build, `check`
-- and parse issues read <sha256>.<type> instead of always <sha256>.pdf. Every document already there is a PDF, so the
-- column starts at 'pdf' and nothing stored changes.
-- The parse issues view names the original by that type: the 0015 view is renamed out of the way (kept, unused) and
-- made again, since a view's definition cannot change in place without REPLACE. An upload that never became a document
-- names its type in the issue's detail (crawler/uploads.py).
-- Additive: a column with a default, a view renamed and a new one with the same grants.
ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS file_type text NOT NULL DEFAULT 'pdf'
  CHECK (file_type ~ '^[a-z0-9]{1,10}$');

ALTER VIEW IF EXISTS control.parse_issues RENAME TO parse_issues_0015;

CREATE VIEW control.parse_issues AS
SELECT i.id, i.kind, i.component, i.state, i.document_id, d.title, m.name AS brand, i.content_sha,
       i.content_sha || '.' || COALESCE(d.file_type, i.detail ->> 'file_type', 'pdf') AS original, d.page_count,
       i.detail, i.times_seen, i.first_seen_at, i.last_seen_at, i.first_version, i.seen_version, i.fixed_version,
       i.fixed_at, i.note
FROM crawl.parse_issues i
LEFT JOIN kb.documents d ON d.id = i.document_id
LEFT JOIN kb.manufacturers m ON m.id = d.manufacturer_id;

GRANT SELECT ON control.parse_issues TO eq_runtime;
GRANT SELECT ON control.parse_issues TO eq_admin;
