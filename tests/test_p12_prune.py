"""`./crawl prune` (2026-10-27, user: nothing is live or in prod yet, so clean up). Every rebuild adds a build next to the
old ones; prune removes section builds that can never be live again. It runs as the migrate role; the runtime role still
cannot delete. Each test works on its own migrate connection and rolls back.
"""
import pytest

from crawler.db import connect
from p3support import q
from test_p12_build import PAGES, document, t, words


@pytest.fixture
def owner_conn():
    conn = connect("migrate", autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def builds(conn, doc):
    return [r[0] for r in q(conn, "SELECT DISTINCT build FROM sections WHERE document_id = %s ORDER BY 1", (doc,))]


def rebuild(conn, doc, monkeypatch, pages):
    import crawler.kb
    monkeypatch.setattr(crawler.kb, "document_pages", lambda c, d, **kw: pages)
    return crawler.kb.build(conn, doc)


def test_prune_keeps_the_live_build_and_removes_older_ones_with_their_terms(owner_conn, monkeypatch):
    from crawler.kb import prune, publish
    doc, _, _ = document(owner_conn)
    rebuild(owner_conn, doc, monkeypatch, PAGES)
    rebuild(owner_conn, doc, monkeypatch, [[t("Charging", 14), t(words(320, "new"))]])
    publish(owner_conn, doc)
    assert builds(owner_conn, doc) == [1, 2]
    gone = prune(owner_conn)
    assert builds(owner_conn, doc) == [2]
    assert gone["sections"] >= 1 and gone["terms"] >= 1
    assert q(owner_conn, "SELECT COUNT(*) FROM section_terms st LEFT JOIN sections s ON s.id = st.section_id "
                         "WHERE s.id IS NULL")[0][0] == 0


def test_prune_never_touches_a_build_newer_than_the_live_one(owner_conn, monkeypatch):
    from crawler.kb import prune, publish
    doc, _, _ = document(owner_conn)
    rebuild(owner_conn, doc, monkeypatch, PAGES)
    publish(owner_conn, doc)
    rebuild(owner_conn, doc, monkeypatch, [[t("Charging", 14), t(words(320, "next"))]])  # built, not yet published
    prune(owner_conn)
    assert builds(owner_conn, doc) == [1, 2]


def test_a_document_never_live_keeps_its_newest_build(owner_conn, monkeypatch):
    from crawler.kb import prune
    doc, _, _ = document(owner_conn, doc_type="not_equipment")
    rebuild(owner_conn, doc, monkeypatch, PAGES)
    rebuild(owner_conn, doc, monkeypatch, [[t("Charging", 14), t(words(320, "other"))]])
    prune(owner_conn)
    assert builds(owner_conn, doc) == [2]


def test_the_runtime_role_still_cannot_delete(runtime_conn):
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        q(runtime_conn, "DELETE FROM sections WHERE false")
