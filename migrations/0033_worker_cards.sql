-- What the admin page's five worker cards show (0032): each worker's brand while a run is going, and no brand when
-- none is, so a run that crashed never leaves a card stuck on a brand. The view reads with its owner's rights, so
-- eq_admin sees this and nothing else in crawl.runs. Additive: a new view and its grants.
CREATE VIEW control.worker_cards AS
SELECT w.worker,
       CASE WHEN r.going THEN w.brand END AS brand,
       CASE WHEN r.going AND w.brand IS NOT NULL THEN w.since END AS since
FROM control.crawl_workers w
CROSS JOIN (SELECT EXISTS (SELECT 1 FROM crawl.runs WHERE state = 'running') AS going) r;

GRANT SELECT ON control.worker_cards TO eq_runtime;
GRANT SELECT ON control.worker_cards TO eq_admin;
