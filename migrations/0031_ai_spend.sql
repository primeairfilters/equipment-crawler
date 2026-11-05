-- The admin page shows what the crawler's AI has cost this month, next to the user's name at the top right (user,
-- 2026-11-04). It is the same total the monthly cap (AI_MONTHLY_CAP_USD) is checked against in crawler/ai.py: every
-- call, including those tied to no brand, which brand_status's per-brand spend leaves out. The view reads with its
-- owner's rights, so eq_admin sees this one number and nothing else in crawl.ai_calls.
-- Additive: a new view and its grants.
CREATE VIEW control.ai_spend AS
SELECT COALESCE(SUM(a.cost_usd), 0) AS spent_this_month
FROM crawl.ai_calls a
WHERE a.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC');

GRANT SELECT ON control.ai_spend TO eq_runtime;
GRANT SELECT ON control.ai_spend TO eq_admin;
