-- The Uploads tab shows each file's size next to its name (user, 2026-11-01). The admin page writes it with the row;
-- uploads from before this column show no size.
-- Additive: a column and a column grant.
ALTER TABLE control.uploads ADD COLUMN IF NOT EXISTS size_bytes bigint NULL CHECK (size_bytes >= 0);

GRANT INSERT (size_bytes) ON control.uploads TO eq_admin;
