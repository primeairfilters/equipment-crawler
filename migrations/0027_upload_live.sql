-- The admin page's Uploads tab shows "Live" once Breeze can use an upload's document (user, 2026-11-01). eq_admin reads
-- nothing in kb, so this view says it per upload: live and not hidden. A queued or rejected upload has no row.
-- Additive: a new view and its grants.
CREATE VIEW control.upload_live AS
SELECT u.id AS upload_id, (d.state = 'live' AND d.hidden_at IS NULL) AS live
FROM control.uploads u
JOIN kb.documents d ON d.id = u.document_id;

GRANT SELECT ON control.upload_live TO eq_runtime;
GRANT SELECT ON control.upload_live TO eq_admin;
