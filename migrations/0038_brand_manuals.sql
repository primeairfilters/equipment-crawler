-- The admin page's Brands tab reads each brand's manual count from here, counted once for every brand together.
-- brand_status's own manuals column counts it brand by brand: about 1.4 s of the view's 1.6 s on prod (357 brands,
-- 2026-11-11), against about 0.3 s for this one pass. Same count: live, not hidden, distinct documents on the
-- brand's pages. A brand with no manuals has no row. Additive: brand_status is not changed.
CREATE VIEW control.brand_manuals AS
SELECT c.id AS brand_id, COUNT(DISTINCT u.document_id) AS manuals
  FROM control.brands c
  JOIN crawl.pages p ON p.brand_id = c.crawl_brand_id
  JOIN crawl.document_urls u ON u.page_id = p.id
  JOIN kb.documents d ON d.id = u.document_id
 WHERE d.state = 'live' AND d.hidden_at IS NULL
 GROUP BY c.id;

GRANT SELECT ON control.brand_manuals TO eq_runtime;
GRANT SELECT ON control.brand_manuals TO eq_admin;
