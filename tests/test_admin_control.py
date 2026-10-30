"""P19 (D75, Q22): the admin page's side of the crawler's database.

- control.brands: the page adds a brand, pauses, resumes, archives it, sets its priority, budget and automatic host
  approval. It never deletes (archive is "remove") and never writes the crawler's own columns.
- control.brand_status, control.host_decisions, control.runs: what the page shows, read only.
- eq_admin can do the above and nothing else: no crawl or kb tables, no schema changes.

Each test runs in a transaction that is rolled back; the eq_admin probes see only their own rows.
"""
import psycopg
import pytest

from p11support import serve, serve_pdf, start, world  # noqa: F401  (world is a fixture)
from p3support import q
from support import connect
from test_p1_users import privileges, refused

INSUFFICIENT = psycopg.errors.InsufficientPrivilege


@pytest.fixture
def admin_conn():
    conn = connect("admin", autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_admin_adds_pauses_and_prioritises_a_brand(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.brands (name, priority, added_by) VALUES ('Probe Brand', 5, 'admin page') "
                    "RETURNING id, state, monthly_budget_usd, auto_hosts, name_key")
        brand_id, state, budget, auto_hosts, key = cur.fetchone()
        assert (state, float(budget), auto_hosts, key) == ("active", 5.0, True, "probe brand")
        cur.execute("UPDATE control.brands SET state = 'paused', priority = 9, monthly_budget_usd = 12, auto_hosts = false, "
                    "note = 'waiting on the rep' WHERE id = %s", (brand_id,))
        cur.execute("UPDATE control.brands SET state = 'archived' WHERE id = %s", (brand_id,))
        cur.execute("SELECT state, priority FROM control.brands WHERE id = %s", (brand_id,))
        assert cur.fetchone() == ("archived", 9)


def test_admin_is_refused_everything_else(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.brands (name) VALUES ('Probe Brand Two') RETURNING id")
        brand_id = cur.fetchone()[0]
    assert refused(admin_conn, f"DELETE FROM control.brands WHERE id = {brand_id}")
    assert refused(admin_conn, f"UPDATE control.brands SET crawl_brand_id = 1 WHERE id = {brand_id}")
    assert refused(admin_conn, f"UPDATE control.brands SET name = 'Renamed' WHERE id = {brand_id}")
    assert refused(admin_conn, "UPDATE control.runs SET state = 'done' WHERE false")
    for table in ("crawl.brands", "crawl.pages", "crawl.runs", "crawl.ai_calls", "kb.documents", "kb.sections"):
        assert refused(admin_conn, f"SELECT 1 FROM {table} LIMIT 0"), table
    assert refused(admin_conn, "CREATE TABLE control.probe (id int)")
    assert refused(admin_conn, "ALTER TABLE control.brands ADD COLUMN probe int")


def test_admin_cannot_write_a_messy_or_duplicate_name(admin_conn):
    for bad in (" Probe", "Probe  Brand", "", "Probe\tBrand"):
        with admin_conn.cursor() as cur:
            cur.execute("SAVEPOINT s")
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO control.brands (name) VALUES (%s)", (bad,))
            cur.execute("ROLLBACK TO SAVEPOINT s")
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.brands (name) VALUES ('Probe Four')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO control.brands (name) VALUES ('PROBE FOUR')")


def test_admin_reads_the_status_views(admin_conn):
    for view in ("control.brand_status", "control.host_decisions", "control.runs"):
        with admin_conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {view} LIMIT 1")
            cur.fetchall()


def test_runtime_reads_and_writes_control_but_never_deletes(migrate_conn):
    assert privileges(migrate_conn, "eq_runtime", "control.brands") == {"SELECT", "INSERT", "UPDATE"}
    assert privileges(migrate_conn, "eq_admin", "control.brands") >= {"SELECT"}
    assert "DELETE" not in privileges(migrate_conn, "eq_admin", "control.brands")
    assert "TRUNCATE" not in privileges(migrate_conn, "eq_admin", "control.brands")
    for view in ("control.brand_status", "control.host_decisions", "control.runs"):
        assert privileges(migrate_conn, "eq_admin", view) == {"SELECT"}, view


def test_breeze_cannot_see_the_control_schema():
    conn = connect("breeze")
    try:
        assert refused(conn, "SELECT 1 FROM control.brands LIMIT 0")
    finally:
        conn.rollback()
        conn.close()


def test_status_shows_state_manuals_cost_and_problems(world):
    serve(world, "/start", "/manual.pdf")
    serve_pdf(world, "/manual.pdf")
    start(world)
    brand_id = q(world.conn, "SELECT id FROM brands WHERE name = %s", (world.brand,))[0][0]
    control_id = q(world.conn, "SELECT id FROM control.brands WHERE crawl_brand_id = %s", (brand_id,))[0][0]

    def status():
        return q(world.conn, "SELECT crawl_state, manuals, spent_this_month, problems FROM control.brand_status "
                             "WHERE id = %s", (control_id,))[0]

    q(world.conn, "UPDATE brands SET discovered_at = now() WHERE id = %s", (brand_id,))
    assert status()[0] == "crawling"  # its start page is queued
    q(world.conn, "INSERT INTO ai_calls (purpose, brand_id, model, input_tokens, cost_usd) "
                  "VALUES ('host_check', %s, 'm', 10, 0.25)", (brand_id,))
    q(world.conn, "INSERT INTO runs (state, finished_at, summary) VALUES ('done', now(), %s)",
      ('{"needs_you": ["' + world.brand + ': files.example.com was stopped after its trial", "Other: x"]}',))
    state, manuals, spent, problems = status()
    assert manuals == 0 and float(spent) == 0.25
    assert problems == [f"{world.brand}: files.example.com was stopped after its trial"]
    q(world.conn, "UPDATE control.brands SET state = 'paused' WHERE id = %s", (control_id,))
    assert status()[0] == "paused"


def test_a_brand_not_yet_seen_by_a_run_is_waiting(world):
    control_id = q(world.conn, "INSERT INTO control.brands (name) VALUES ('Brand Nobody Crawled') RETURNING id")[0][0]
    assert q(world.conn, "SELECT crawl_state, manuals FROM control.brand_status WHERE id = %s",
             (control_id,))[0] == ("waiting", 0)
