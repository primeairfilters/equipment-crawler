-- Tags set by hand (`./crawl tag`): the names of the fields (doc_type, equipment_type, trade) the user set on this
-- document. The AI tagging it again leaves those fields alone. Additive: a column with an empty default.
ALTER TABLE kb.documents ADD COLUMN IF NOT EXISTS user_tags text[] NOT NULL DEFAULT '{}';
