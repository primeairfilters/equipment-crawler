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
    assert sections_of(runtime_conn, doc) == tuple((i, 1, crawler.kb.build_version()) for i in ids)  # same rows, vectors
    assert crawler.kb.build_version().startswith("test-next/")
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
    assert parser().parse_args(["sections"]).workers == 3  # measured 2026-10-28: 2.4x one worker, 1.1 GB peak


def test_a_new_pdf_reader_alone_makes_sections_look_at_every_document_again(runtime_conn, monkeypatch):
    """A reader change (pdfchild.py, pdfplumber) re-reads PDFs through the layout cache; the build version carries the
    reader's key, so `sections` takes every document again (an unchanged one only takes the new version)."""
    import crawler.kb
    import crawler.textextract
    from crawler.kb import build_and_publish
    doc, _, _ = document(runtime_conn)
    monkeypatch.setattr(crawler.kb, "document_pages", lambda conn, d, **kw: PAGES)
    build_and_publish(runtime_conn)
    before = sections_of(runtime_conn, doc)
    monkeypatch.setattr(crawler.textextract, "_layout_reader", lambda: "a newer reader")
    summary = build_and_publish(runtime_conn)
    after = sections_of(runtime_conn, doc)
    assert summary["unchanged"] >= 1 and [r[0] for r in after] == [r[0] for r in before]
    assert {r[2] for r in after} == {crawler.kb.build_version()} != {r[2] for r in before}


# ---------------------------------------------------------------- review fixes (2026-10-28)

def live_with_a_library_file(conn, tmp_path, monkeypatch):
    """A live document whose PDF is in the library, first built from PAGES."""
    import crawler.kb
    from crawler.kb import build_and_publish
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    sha = "e" * 64
    (tmp_path / f"{sha}.pdf").write_bytes(b"%PDF")
    doc, _, _ = document(conn, sha=sha)
    monkeypatch.setattr(crawler.kb, "extract_layout", lambda data, **kw: PAGES)
    build_and_publish(conn)
    return doc


def test_a_live_document_whose_pdf_cannot_be_read_now_keeps_its_live_build(runtime_conn, tmp_path, monkeypatch):
    import crawler.kb
    from crawler.kb import build_and_publish
    from crawler.textextract import ExtractError
    doc = live_with_a_library_file(runtime_conn, tmp_path, monkeypatch)
    before = sections_of(runtime_conn, doc)

    def too_slow(data, **kwargs):
        raise ExtractError("OCR took too long")

    monkeypatch.setattr(crawler.kb, "extract_layout", too_slow)
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    summary = build_and_publish(runtime_conn)
    assert sections_of(runtime_conn, doc) == before  # no build from the stored text, the live one stays
    assert (doc, "OCR took too long") in summary["kept"] and summary["fallback"] == []


def test_ocr_that_fails_while_reading_the_layout_is_an_error_not_empty_pages(monkeypatch):
    import pytest
    import crawler.textextract
    from crawler.textextract import ExtractError
    monkeypatch.setattr(crawler.textextract, "_run_child", lambda *a, **k: {"layout": [[]], "rows": None})

    def ocr_timeout(*args, **kwargs):
        raise ExtractError("the PDF took too long to read")

    monkeypatch.setattr(crawler.textextract, "_ocr_child", ocr_timeout)
    monkeypatch.setattr(__import__("shutil"), "which", lambda name: "/usr/bin/tesseract")
    with pytest.raises(ExtractError):
        crawler.textextract.extract_layout(b"%PDF-1")


def test_a_live_document_that_now_gives_no_sections_is_reported_not_counted_unchanged(runtime_conn, tmp_path,
                                                                                        monkeypatch):
    import crawler.kb
    from crawler.kb import build_and_publish
    doc = live_with_a_library_file(runtime_conn, tmp_path, monkeypatch)
    monkeypatch.setattr(crawler.kb, "extract_layout", lambda data, **kw: [[]])
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    summary = build_and_publish(runtime_conn)
    assert summary["unchanged"] == 0 and summary["empty"] >= 1
    assert {b for b, _ in live(runtime_conn, doc)} == {1}


def many_documents_with_files(conn, tmp_path, monkeypatch, n):
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    docs = []
    for k in range(n):
        sha = f"{k + 100:064x}"
        (tmp_path / f"{sha}.pdf").write_bytes(b"%PDF")
        docs.append(document(conn, sha=sha)[0])
    return docs


def test_originals_are_read_only_a_few_documents_ahead_of_the_database_work(runtime_conn, tmp_path, monkeypatch):
    import crawler.kb
    from crawler.kb import build_and_publish
    many_documents_with_files(runtime_conn, tmp_path, monkeypatch, 12)
    lock, read, built, ahead = threading.Lock(), [0], [0], [0]

    def counting_layout(data, **kwargs):
        with lock:
            read[0] += 1
            ahead[0] = max(ahead[0], read[0] - built[0])
        return PAGES

    real = crawler.kb._build_one

    def counting_build(*args):
        time.sleep(0.02)  # the database work is the slow side, as with the layout cache warm
        real(*args)
        with lock:
            built[0] += 1

    monkeypatch.setattr(crawler.kb, "extract_layout", counting_layout)
    monkeypatch.setattr(crawler.kb, "_build_one", counting_build)
    build_and_publish(runtime_conn, workers=2)
    assert ahead[0] <= 2 * 2 + 1


def test_an_error_in_the_database_work_stops_the_reading_too(runtime_conn, tmp_path, monkeypatch):
    import pytest
    import crawler.kb
    from crawler.kb import build_and_publish
    many_documents_with_files(runtime_conn, tmp_path, monkeypatch, 12)
    read = []

    def slow_layout(data, **kwargs):
        time.sleep(0.05)
        read.append(1)
        return PAGES

    def broken(*args):
        raise RuntimeError("database gone")

    monkeypatch.setattr(crawler.kb, "extract_layout", slow_layout)
    monkeypatch.setattr(crawler.kb, "_build_one", broken)
    with pytest.raises(RuntimeError):
        build_and_publish(runtime_conn, workers=2)
    assert len(read) < 12  # the queued reads were dropped, not all waited for


def test_the_pdf_reader_starts_without_preexec_fn_since_sections_starts_readers_from_threads(monkeypatch):
    """preexec_fn runs Python in the forked child and is not safe when the parent has threads (a worker could hang
    before its timeout starts); the child sets its own memory limit instead."""
    import subprocess
    import crawler.textextract
    seen = {}
    real = subprocess.Popen

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    from p3support import make_pdf
    crawler.textextract.extract_layout(make_pdf([["Installation Manual"]]))
    assert seen.get("preexec_fn") is None and seen["env"]["CHILD_MEMORY_BYTES"]


def test_the_memory_limit_still_stops_a_reader(monkeypatch):
    import pytest
    from crawler.textextract import ExtractError, _run_child
    from p3support import make_pdf
    with pytest.raises(ExtractError):
        _run_child(make_pdf([["Installation Manual"]]), ["200", "layout"], 60, 40 * 1024 ** 2)


def test_the_build_version_follows_library_versions_and_the_ocr_reader(monkeypatch):
    import crawler.kb
    import crawler.textextract
    first = crawler.kb.build_version()
    real = crawler.textextract._layout_reader()
    monkeypatch.setattr(crawler.textextract, "_layout_reader", lambda: real + " (pdfplumber upgraded)")
    second = crawler.kb.build_version()
    monkeypatch.setattr(crawler.textextract, "_ocr_reader", lambda: "an OCR change")
    assert len({first, second, crawler.kb.build_version()}) == 3


def test_a_changed_ocr_reader_reads_scanned_pages_again(tmp_path, monkeypatch):
    import crawler.textextract
    from test_p12_ocr import BODY, picture_page, picture_pdf
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    pdf = picture_pdf(picture_page("Refrigerant Charging", BODY))
    crawler.textextract.extract_pdf(pdf)
    calls = []
    real = crawler.textextract._ocr_child
    monkeypatch.setattr(crawler.textextract, "_ocr_child", lambda *a: calls.append(1) or real(*a))
    monkeypatch.setattr(crawler.textextract, "_ocr_reader", lambda: "an OCR change")
    crawler.textextract.extract_pdf(pdf)
    assert calls == [1]


def test_an_ocr_result_half_written_never_damages_the_kept_one(tmp_path, monkeypatch):
    import json
    import pathlib
    import crawler.textextract
    from test_p12_ocr import BODY, picture_page, picture_pdf
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    two = picture_pdf(picture_page("Refrigerant Charging", BODY), picture_page("Wiring", BODY))
    crawler.textextract.ocr_pages(two, [0])
    kept = next(tmp_path.joinpath(".ocr").glob("*.json"))
    good = json.loads(kept.read_text())
    real = pathlib.Path.write_text

    def dies_halfway(self, text, *args, **kwargs):
        real(self, text[: len(text) // 2], *args, **kwargs)
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "write_text", dies_halfway)
    crawler.textextract.ocr_pages(two, [1])
    assert json.loads(kept.read_text()) == good
