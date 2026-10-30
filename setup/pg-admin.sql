-- The login Tracforce's /admin page uses (P19, D77). Run by setup/pg-admin.sh as the server's superuser.
-- Adds only: one role and connect rights on both databases. What it may read and write (the control schema) is granted
-- by migrations, so the testing database gets the same rights. The password arrives as a psql variable (admin_pw).
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE eq_admin LOGIN PASSWORD %L', :'admin_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eq_admin') \gexec

GRANT CONNECT ON DATABASE tracforce_equipment TO eq_admin;
GRANT CONNECT ON DATABASE tracforce_equipment_testing TO eq_admin;
