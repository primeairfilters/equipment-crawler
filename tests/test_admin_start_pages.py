"""Pages the user adds on the admin page's Hosts tab (control.start_pages, user 2026-11-13).

- The page writes a brand and an address; it cannot write what the crawler fills (start_url_id, refused) or delete.
- The next run's sync makes each one a start address of its brand, approves its host for all its pages and records
  the user's host decision, so it shows on the Hosts tab like the crawler's own. A refused one keeps why.
- control.start_page_status says where each one is: waiting, crawling, crawled, failed or refused.

Each test runs in a transaction that is rolled back.
"""
import psycopg
import pytest

from p11support import crawl, serve, world  # noqa: F401  (world is a fixture)
from p3support import q
from support import connect
from test_p1_users import privileges, refused


@pytest.fixture
def admin_conn():
    conn = connect("admin", autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _control_id(world):
    return q(world.conn, "SELECT id FROM control.brands WHERE crawl_brand_id = %s", (world.brand_id,))[0][0]


def _add(world, url):
    return q(world.conn, "INSERT INTO control.start_pages (brand_id, url, added_by) VALUES (%s, %s, 'admin page') "
                         "RETURNING id", (_control_id(world), url))[0][0]


def _status(world, page_id):
    return q(world.conn, "SELECT state, host, refused FROM control.start_page_status WHERE id = %s", (page_id,))[0]


def test_admin_adds_a_page_but_cannot_write_the_crawlers_columns(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.brands (name) VALUES ('Probe Pages Brand') RETURNING id")
        brand_id = cur.fetchone()[0]
        cur.execute("INSERT INTO control.start_pages (brand_id, url, added_by) VALUES (%s, 'https://docs.probe.test/x', "
                    "'admin page') RETURNING id", (brand_id,))
        page_id = cur.fetchone()[0]
        cur.execute("SELECT state, host FROM control.start_page_status WHERE id = %s", (page_id,))
        assert cur.fetchone() == ("waiting", "docs.probe.test")
    assert refused(admin_conn, f"UPDATE control.start_pages SET start_url_id = 1 WHERE id = {page_id}")
    assert refused(admin_conn, f"UPDATE control.start_pages SET refused = 'x' WHERE id = {page_id}")
    assert refused(admin_conn, f"DELETE FROM control.start_pages WHERE id = {page_id}")


def test_grants(migrate_conn):
    assert privileges(migrate_conn, "eq_admin", "control.start_page_status") == {"SELECT"}
    assert "DELETE" not in privileges(migrate_conn, "eq_admin", "control.start_pages")
    assert "DELETE" not in privileges(migrate_conn, "eq_runtime", "control.start_pages")


def test_the_database_refuses_an_address_that_is_not_a_web_address(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.brands (name) VALUES ('Probe Bad Pages') RETURNING id")
        brand_id = cur.fetchone()[0]
    for bad in ("", "docs.probe.test/x", "ftp://docs.probe.test/x", "https://docs probe.test/x", "https:///x"):
        with admin_conn.cursor() as cur:
            cur.execute("SAVEPOINT s")
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO control.start_pages (brand_id, url) VALUES (%s, %s)", (brand_id, bad))
            cur.execute("ROLLBACK TO SAVEPOINT s")


def test_the_next_run_crawls_the_page_and_its_host_shows_as_the_users(world):
    from crawler.control import sync
    url = world.url("docs.other.test", "/manuals")
    page_id = _add(world, url)
    assert _status(world, page_id) == ("waiting", "docs.other.test", None)

    sync(world.conn)
    sync(world.conn)  # a second run does not add it again
    assert q(world.conn, "SELECT found_by, status FROM start_urls WHERE brand_id = %s AND url = %s",
             (world.brand_id, url)) == (("user", "active"),)
    assert q(world.conn, "SELECT status, scope FROM brand_domains WHERE brand_id = %s AND host = 'docs.other.test'",
             (world.brand_id,)) == (("approved", "all"),)
    assert q(world.conn, "SELECT state, decided_by FROM control.host_decisions WHERE brand_id = %s "
                         "AND host = 'docs.other.test'", (_control_id(world),)) == (("confirmed", "user"),)
    assert _status(world, page_id)[0] == "crawling"

    serve(world, "/manuals", host="docs.other.test")
    crawl(world)
    assert _status(world, page_id)[0] == "crawled"


def test_the_users_page_overrules_a_host_the_judge_stopped(world):
    from crawler.control import sync
    q(world.conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, scope) "
                  "VALUES (%s, 'files.stopped.test', 'stopped', 'ai', 'pdf')", (world.brand_id,))
    q(world.conn, "INSERT INTO host_decisions (brand_id, host, state, decided_by, reason) "
                  "VALUES (%s, 'files.stopped.test', 'stopped', 'ai', 'trial failed')", (world.brand_id,))
    _add(world, world.url("files.stopped.test", "/library"))
    sync(world.conn)
    assert q(world.conn, "SELECT status, scope FROM brand_domains WHERE brand_id = %s AND host = 'files.stopped.test'",
             (world.brand_id,)) == (("approved", "all"),)
    assert q(world.conn, "SELECT state, decided_by FROM host_decisions WHERE brand_id = %s "
                         "AND host = 'files.stopped.test'", (world.brand_id,)) == (("confirmed", "user"),)


def test_a_refused_website_is_not_crawled_and_says_why(world):
    from crawler.control import sync
    page_id = _add(world, "https://www.ahridirectory.org/search")
    sync(world.conn)
    state, host, why = _status(world, page_id)
    assert (state, host) == ("refused", "www.ahridirectory.org")
    assert why == "That website is on the never-crawl list."
    assert q(world.conn, "SELECT 1 FROM start_urls WHERE brand_id = %s AND url LIKE '%%ahridirectory%%'",
             (world.brand_id,)) == ()
    assert q(world.conn, "SELECT 1 FROM brand_domains WHERE brand_id = %s AND host LIKE '%%ahridirectory%%'",
             (world.brand_id,)) == ()
