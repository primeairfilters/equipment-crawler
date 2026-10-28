-- A read-only login for looking at the data in a desktop client (DBeaver). Run by setup/pg-readonly.sh as the
-- server's superuser. Adds only: one role, connect rights, SELECT on crawl and kb (and on tables migrate adds later).
-- The password arrives as a psql variable (readonly_pw) and is never written here.
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE eq_readonly LOGIN PASSWORD %L', :'readonly_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eq_readonly') \gexec
ALTER ROLE eq_readonly SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE tracforce_equipment TO eq_readonly;
\connect tracforce_equipment
GRANT USAGE ON SCHEMA crawl, kb TO eq_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA crawl, kb TO eq_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE eq_migrate IN SCHEMA crawl, kb GRANT SELECT ON TABLES TO eq_readonly;
