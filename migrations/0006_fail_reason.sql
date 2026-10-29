-- Why a page's last fetch failed (not_pdf, http_404, too_big, network, ...). Before, the reason was only in the run's
-- summary: three empty Ruud PDFs in batch 4 needed outside requests to find out why. Additive: a nullable column.
ALTER TABLE crawl.pages ADD COLUMN IF NOT EXISTS fail_reason varchar(40) NULL;
