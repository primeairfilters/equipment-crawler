"""P1 (P10 on PostgreSQL): what each of the three roles may do.

- eq_runtime: SELECT/INSERT/UPDATE on crawl and kb, never DELETE or TRUNCATE, never changes the schema.
- eq_migrate: builds and alters the schemas.
- eq_breeze: SELECT on kb only, nothing in crawl, never writes.

Nothing here empties or deletes anything. The DELETE probe is `WHERE false` inside a transaction that is
rolled back, and PostgreSQL checks the privilege before it looks at rows. TRUNCATE is checked in the catalog
only, never tried.
"""
import psycopg
import pytest

from support import TESTING_DB, connect

INSUFFICIENT = psycopg.errors.InsufficientPrivilege


def tables(conn, schemas=("crawl", "kb")):
    with conn.cursor() as cur:
        cur.execute("SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY 1",
                    (list(schemas),))
        return [r[0] for r in cur.fetchall()]


def privileges(conn, role, table):
    with conn.cursor() as cur:
        cur.execute("SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p "
                    "WHERE has_table_privilege(%s, %s, p)", (role, table))
        return {r[0] for r in cur.fetchall()}


def refused(conn, sql):
    """True when the statement is refused for lack of privilege. Runs in a savepoint so the transaction goes on."""
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT probe")
        try:
            cur.execute(sql)
        except INSUFFICIENT:
            cur.execute("ROLLBACK TO SAVEPOINT probe")
            return True
        cur.execute("RELEASE SAVEPOINT probe")
        return False


def test_every_table_gives_runtime_select_insert_update_only(migrate_conn):
    names = tables(migrate_conn)
    assert "crawl.schema_migrations" in names and "kb.sections" in names, names
    for table in names:
        if table == "crawl.schema_migrations":
            continue
        assert privileges(migrate_conn, "eq_runtime", table) == {"SELECT", "INSERT", "UPDATE"}, table


def test_runtime_is_refused_delete_on_every_table(runtime_conn):
    names = tables(runtime_conn)
    assert "crawl.pages" in names, f"migrate has not built the schema yet: {names}"
    for table in names:
        if table == "crawl.schema_migrations":
            continue
        with runtime_conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # control: it can read the table, so the refusal is about DELETE
            cur.fetchone()
        assert refused(runtime_conn, f"DELETE FROM {table} WHERE false"), f"DELETE was NOT refused on {table}"


def test_runtime_has_no_truncate_anywhere(migrate_conn):
    names = tables(migrate_conn)
    assert names
    for table in names:
        assert "TRUNCATE" not in privileges(migrate_conn, "eq_runtime", table), table


def test_runtime_cannot_change_the_schema(runtime_conn):
    assert refused(runtime_conn, "CREATE TABLE crawl.runtime_probe (id int)")
    assert refused(runtime_conn, "CREATE TABLE kb.runtime_probe (id int)")
    assert refused(runtime_conn, "ALTER TABLE crawl.pages ADD COLUMN runtime_probe int")


def test_migrate_role_owns_the_schemas(migrate_conn):
    with migrate_conn.cursor() as cur:
        cur.execute("SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname IN ('crawl', 'kb') ORDER BY 1")
        assert cur.fetchall() == (("crawl", "eq_migrate"), ("kb", "eq_migrate"))


def test_breeze_can_read_kb_only():
    conn = connect("breeze")
    try:
        names = tables(conn, ("kb",))
        assert "kb.sections" in names and "kb.documents" in names, names
        for table in names:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                cur.fetchone()
            assert privileges(conn, "eq_breeze", table) == {"SELECT"}, table
        assert refused(conn, "INSERT INTO kb.manufacturers (name, name_key) SELECT 'x', 'x' WHERE false")
        assert refused(conn, "UPDATE kb.sections SET is_live = false WHERE false")
        for table in ("crawl.pages", "crawl.page_text", "crawl.ai_calls", "crawl.brands"):
            assert refused(conn, f"SELECT 1 FROM {table} LIMIT 0"), table
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("kind", ["runtime", "breeze"])
def test_only_the_equipment_databases_are_open_to_the_crawler_roles(kind):
    """The two databases refuse every role but the three crawler ones (no PUBLIC connect)."""
    conn = connect("migrate", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT has_database_privilege('public', %s, 'CONNECT')", (TESTING_DB,))
            assert cur.fetchone()[0] is False
            cur.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')", (f"eq_{kind}", TESTING_DB))
            assert cur.fetchone()[0] is True
    finally:
        conn.close()
