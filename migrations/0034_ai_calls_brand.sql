-- The admin page's Brands tab (control.brand_status) sums each brand's AI spend this month. Without this index it read
-- every AI call of the month once per brand: 1.75 s of the tab's load on prod (357 brands, 21,543 calls, 2026-11-08).
-- On the partitioned table, so every month's partition gets it, the ones made later too. Additive.
CREATE INDEX IF NOT EXISTS ai_calls_brand ON crawl.ai_calls (brand_id, created_at);
