-- A read-only login for Claude's checks of the prod crawler database (P22; mirrors the app's MariaDB claude_ro).
-- Run by setup/pg-claude-ro.sh as the server's superuser. Adds only: one role, connect rights, SELECT on crawl, kb,
-- control and breeze (and on tables migrate adds later), every transaction read-only, 10 s per statement.
-- The password arrives as a psql variable (claude_pw) and is never written here.
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE claude_ro LOGIN PASSWORD %L', :'claude_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claude_ro') \gexec
ALTER ROLE claude_ro SET default_transaction_read_only = on;
ALTER ROLE claude_ro SET statement_timeout = '10s';
ALTER ROLE claude_ro CONNECTION LIMIT 2;

GRANT CONNECT ON DATABASE tracforce_equipment TO claude_ro;
\connect tracforce_equipment
GRANT USAGE ON SCHEMA crawl, kb, control, breeze TO claude_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA crawl, kb, control, breeze TO claude_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE eq_migrate IN SCHEMA crawl, kb, control, breeze GRANT SELECT ON TABLES TO claude_ro;
