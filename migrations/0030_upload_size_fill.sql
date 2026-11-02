-- Uploads from before 0029 get their size from the stored original (user, 2026-11-01): the size crawl.files recorded
-- for the upload's document. An upload that never became a document (rejected) has no stored original and keeps none.
-- Additive: fills an empty column only; nothing already set changes.
UPDATE control.uploads u
SET size_bytes = f.size_bytes
FROM kb.documents d
JOIN crawl.files f ON f.sha256 = d.content_sha
WHERE d.id = u.document_id AND u.size_bytes IS NULL;
