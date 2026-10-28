-- The one-time root step for the PostgreSQL server (D64). Run by setup/pg-setup.sh as the server's superuser.
-- Adds only: roles, the two databases, extensions and connect rights. Re-running changes nothing that exists.
-- Passwords arrive as psql variables (migrate_pw, runtime_pw, breeze_pw) and are never written here.
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE eq_migrate LOGIN PASSWORD %L', :'migrate_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eq_migrate') \gexec
SELECT format('CREATE ROLE eq_runtime LOGIN PASSWORD %L', :'runtime_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eq_runtime') \gexec
SELECT format('CREATE ROLE eq_breeze LOGIN PASSWORD %L', :'breeze_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eq_breeze') \gexec

SELECT 'CREATE DATABASE tracforce_equipment'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'tracforce_equipment') \gexec
SELECT 'CREATE DATABASE tracforce_equipment_testing'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'tracforce_equipment_testing') \gexec

\connect tracforce_equipment
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE CONNECT, TEMPORARY ON DATABASE tracforce_equipment FROM PUBLIC;
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE tracforce_equipment TO eq_migrate;
GRANT CONNECT ON DATABASE tracforce_equipment TO eq_runtime, eq_breeze;

\connect tracforce_equipment_testing
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE CONNECT, TEMPORARY ON DATABASE tracforce_equipment_testing FROM PUBLIC;
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE tracforce_equipment_testing TO eq_migrate;
GRANT CONNECT ON DATABASE tracforce_equipment_testing TO eq_runtime, eq_breeze;
