-- A parse issue can be 'needs_password' (user, 2026-11-08): a PDF its maker locked with an open password (21
-- ClimateMaster files). No code change can read it, so it stays off the fix list and is never retried; the admin
-- page shows it in red. The state check is replaced by the same list plus the new state. Additive.
ALTER TABLE crawl.parse_issues DROP CONSTRAINT IF EXISTS parse_issues_state_check;
ALTER TABLE crawl.parse_issues ADD CONSTRAINT parse_issues_state_check
  CHECK (state IN ('open', 'retrying', 'fixed', 'wont_fix', 'needs_password'));
