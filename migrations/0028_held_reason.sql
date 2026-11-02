-- The Uploads tab shows "Held back" with the reason when the crawler refused to put an upload's document live (user,
-- 2026-11-01): a document tagged not equipment, or whose trade is not one of its brand's trades. kb.publish keeps the
-- reason it refused in held_reason and clears it when the document goes live.
-- control.upload_live gains the reason: the 0027 view is renamed out of the way (kept, unused) and made again, since a
-- view's definition cannot change in place without REPLACE.
-- Additive: a column, a view renamed and a new one with the same grants.
ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS held_reason text NULL;

ALTER VIEW IF EXISTS control.upload_live RENAME TO upload_live_0027;

CREATE VIEW control.upload_live AS
SELECT u.id AS upload_id, (d.state = 'live' AND d.hidden_at IS NULL) AS live,
       CASE WHEN d.state <> 'live' THEN d.held_reason END AS held_reason
FROM control.uploads u
JOIN kb.documents d ON d.id = u.document_id;

GRANT SELECT ON control.upload_live TO eq_runtime;
GRANT SELECT ON control.upload_live TO eq_admin;
