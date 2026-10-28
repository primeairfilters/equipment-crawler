"""Faster rebuilds without a smaller sample (user, 2026-10-28). A new sectioner version makes a new build only for a
document whose sections come out different (text, pages, path, kind, flags or search terms); an unchanged one keeps its
live build, its rows and its vectors, and only takes the new version. `sections` reads the originals of several
documents at once (workers), while the database work stays in order on one connection.

Rows are rolled back after each test."""
import threading
import time

from p3support import q
from test_p12_build import PAGES, document, live, t, words


def sections_of(conn, doc):
    return q(conn, "SELECT id, build, sectioner_version FROM sections WHERE document_id = %s ORDER BY id", (doc,))


def test_an_unchanged_document_keeps_its_live_build_under_a_new_sectioner_version(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build, publish
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    ids = [r[0] for r in sections_of(runtime_conn, doc)]
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    assert build(runtime_conn, doc, pages=PAGES) is None
    assert sections_of(runtime_conn, doc) == tuple((i, 1, "test-next") for i in ids)  # same rows, so same vectors
    assert {b for b, _ in live(runtime_conn, doc)} == {1}


def test_a_new_sectioner_version_that_changes_a_flag_makes_a_new_build(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    flagged = [[dict(i, ocr=True) if i["kind"] == "text" else i for i in page] for page in PAGES]
    assert build(runtime_conn, doc, pages=flagged) == 2


def test_a_new_sectioner_version_that_changes_the_search_terms_makes_a_new_build(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    real = crawler.kb.terms
    monkeypatch.setattr(crawler.kb, "terms", lambda text: real(text) | {"NEWTERM"})
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    assert build(runtime_conn, doc, pages=PAGES) == 2


def test_sections_rebuilds_only_the_documents_that_changed(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build_and_publish
    same, _, _ = document(runtime_conn)
    changed, _, _ = document(runtime_conn)
    other = [[t("Refrigerant Charging", 14), t(words(330) + " Error E5 means HI PS tripped.")], PAGES[1]]
    pages = {same: PAGES, changed: PAGES}
    monkeypatch.setattr(crawler.kb, "document_pages", lambda conn, d, **kw: pages.get(d, []))
    build_and_publish(runtime_conn)
    pages[changed] = other
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    summary = build_and_publish(runtime_conn)
    assert {b for b, _ in live(runtime_conn, same)} == {1} and {b for b, _ in live(runtime_conn, changed)} == {2}
    assert summary["unchanged"] >= 1 and summary["built"] >= 1
    assert build_and_publish(runtime_conn)["built"] == 0  # every document is on the new version now


def test_originals_are_read_several_at_a_time_and_give_the_same_sections(runtime_conn, tmp_path, monkeypatch):
    import crawler.kb
    from crawler.kb import build_and_publish
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    docs = []
    for n in range(4):
        sha = f"{n:064x}"
        (tmp_path / f"{sha}.pdf").write_bytes(b"%PDF")
        docs.append(document(runtime_conn, sha=sha)[0])
    lock, running, most = threading.Lock(), [0], [0]

    def slow_layout(data, **kwargs):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.3)
        with lock:
            running[0] -= 1
        return PAGES

    monkeypatch.setattr(crawler.kb, "extract_layout", slow_layout)
    summary = build_and_publish(runtime_conn, workers=3)
    assert most[0] > 1 and summary["built"] >= 4
    bodies = {d: q(runtime_conn, "SELECT body_sha FROM sections WHERE document_id = %s ORDER BY section_no", (d,))
              for d in docs}
    assert len(set(bodies.values())) == 1 and all(live(runtime_conn, d) for d in docs)


def test_an_original_that_fails_in_a_worker_falls_back_to_the_stored_text_and_is_reported(runtime_conn, tmp_path,
                                                                                           monkeypatch):
    import crawler.kb
    from crawler.kb import build_and_publish
    from crawler.textextract import ExtractError
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    sha = "f" * 64
    (tmp_path / f"{sha}.pdf").write_bytes(b"%PDF")
    doc, _, _ = document(runtime_conn, sha=sha)

    def too_slow(data, **kwargs):
        raise ExtractError("the PDF took too long to read")

    monkeypatch.setattr(crawler.kb, "extract_layout", too_slow)
    summary = build_and_publish(runtime_conn, workers=2)
    assert (doc, "the PDF took too long to read") in summary["fallback"]


def test_the_sections_command_takes_a_worker_count():
    from crawler.__main__ import build_parser as parser
    assert parser().parse_args(["sections", "Ruud", "--workers", "3"]).workers == 3
    assert parser().parse_args(["sections"]).workers == 2
