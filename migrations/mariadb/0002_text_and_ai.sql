-- P3: where a page hangs in the tree, how many text pages it has, and the AI batch state.
ALTER TABLE pages
  ADD COLUMN IF NOT EXISTS parent_id BIGINT UNSIGNED NULL,
  ADD COLUMN IF NOT EXISTS page_count INT UNSIGNED NULL,
  ADD COLUMN IF NOT EXISTS ai_batch_id VARCHAR(100) NULL,
  ADD COLUMN IF NOT EXISTS ai_pending_sha CHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS ai_parts INT UNSIGNED NULL,
  ADD COLUMN IF NOT EXISTS ai_model VARCHAR(60) NULL,
  ADD COLUMN IF NOT EXISTS ai_reserved_usd DECIMAL(12,6) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS ai_retry_at DATETIME NULL;

CREATE INDEX IF NOT EXISTS pages_parent ON pages (parent_id);

CREATE INDEX IF NOT EXISTS pages_content_sha ON pages (content_sha);

CREATE INDEX IF NOT EXISTS pages_ai_extracted_sha ON pages (ai_extracted_sha);

CREATE INDEX IF NOT EXISTS pages_ai_batch ON pages (ai_batch_id);

CREATE INDEX IF NOT EXISTS page_text_sha ON page_text (text_sha);
