"""P6: a fetched PDF is kept in the document library as <sha256>.pdf, once, and never replaced (D26, D27).

Same fake world as test_p2_run.py: an in-process server, a fake clock, rolled-back rows. The
library folder is pytest's tmp_path, handed to the tool through the DOCS_DIR environment
variable (read when a document is saved, not at import). Nothing here touches the real library.
"""
import hashlib
import os
import re

import pytest

from test_p2_fetch import World
from test_p2_run import crawl, page, paths, serve, start

PDF_HEADERS = {"Content-Type": "application/pdf"}
HTML_HEADERS = {"Content-Type": "text/html"}
MONTH_AND_A_DAY = 31 * 24 * 3600  # past RECHECK_AFTER (30 days), so the page is due again


def pdf_bytes(tag):
    return f"%PDF-1.4\n{tag} RA15AZ spec sheet\n%%EOF\n".encode()


def sha(body):
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def world(runtime_conn):
    w = World(runtime_conn)
    w.approve("ruud.test")
    try:
        yield w
    finally:
        w.site.stop()


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """An empty library folder, and DOCS_DIR pointing at it."""
    folder = tmp_path / "docs"
    folder.mkdir()
    monkeypatch.setenv("DOCS_DIR", str(folder))
    return folder


def use_library(monkeypatch, folder):
    folder.mkdir(exist_ok=True)
    monkeypatch.setenv("DOCS_DIR", str(folder))
    return folder


def files(folder):
    """Every regular file under the folder, as sorted paths relative to it. Read only."""
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            found.append(os.path.relpath(os.path.join(root, name), folder))
    return sorted(found)


def library_pdfs(folder):
    return [f for f in files(folder) if f.endswith(".pdf") and os.sep not in f]


def serve_pdf(world, path, body, **headers):
    world.site.route(path, body, headers={**PDF_HEADERS, **headers})


def pdf_hits(world, path):
    return [r for r in world.site.page_hits() if r["path"] == path]


def one(world, sql, args=()):
    return world.one(sql, args)


# ---------------------------------------------------------------- saving a fetched PDF


def test_a_fetched_pdf_is_stored_as_its_sha256_and_the_name_matches_the_page_row(world, docs):
    body = pdf_bytes("one")
    start(world)
    serve(world, "/start", "/doc.pdf")
    serve_pdf(world, "/doc.pdf", body)
    crawl(world)
    assert library_pdfs(docs) == [f"{sha(body)}.pdf"]
    assert (docs / f"{sha(body)}.pdf").read_bytes() == body
    assert one(world, "SELECT content_sha FROM pages WHERE brand_id = %s AND url LIKE '%%doc.pdf'",
               (world.brand_id,)) == ((sha(body),),)


def test_two_pdfs_are_two_files_and_the_same_bytes_at_two_urls_are_one(world, docs):
    a, b = pdf_bytes("a"), pdf_bytes("b")
    start(world)
    serve(world, "/start", "/a.pdf", "/b.pdf", "/copy-of-a.pdf")
    serve_pdf(world, "/a.pdf", a)
    serve_pdf(world, "/b.pdf", b)
    serve_pdf(world, "/copy-of-a.pdf", a)
    crawl(world)
    assert len(pdf_hits(world, "/copy-of-a.pdf")) == 1  # control: the duplicate really was fetched
    assert library_pdfs(docs) == sorted([f"{sha(a)}.pdf", f"{sha(b)}.pdf"])


def test_the_file_name_comes_only_from_the_hash_never_from_the_url(world, docs):
    body = pdf_bytes("sneaky")
    start(world)
    serve(world, "/start", "/..%2f..%2fevil.pdf", "/a/../../b.pdf")
    serve_pdf(world, "/..%2f..%2fevil.pdf", body)
    crawl(world)
    assert len(world.site.page_hits()) >= 2  # control: the odd URL was requested
    saved = files(docs)
    assert saved, "nothing was saved, so the file names were never checked"
    assert all(re.fullmatch(r"(\.parts/.*|[0-9a-f]{64}\.(pdf|html))", f) for f in saved), saved  # HTML too since D57
    assert library_pdfs(docs) == [f"{sha(body)}.pdf"]
    assert not (docs.parent / "evil.pdf").exists() and not (docs.parent.parent / "evil.pdf").exists()


def test_html_pages_are_kept_as_html_next_to_the_pdf(world, docs):
    """Changed by D57 (2026-10-22): HTML originals are kept too, as <sha>.html, under the same never-replaced rule."""
    body = pdf_bytes("only the pdf")
    start(world)
    serve(world, "/start", "/product", "/doc.pdf")
    serve(world, "/product", "/doc.pdf")
    serve_pdf(world, "/doc.pdf", body)
    crawl(world)
    assert pdf_hits(world, "/product") and pdf_hits(world, "/start")  # control: the HTML pages were fetched
    assert library_pdfs(docs) == [f"{sha(body)}.pdf"]
    kept = sorted(f for f in files(docs) if f.endswith(".html") and not f.startswith(".parts"))
    assert kept == sorted({f"{sha(page('/product', '/doc.pdf', title='/start'))}.html",
                           f"{sha(page('/doc.pdf', title='/product'))}.html"}), kept


# ---------------------------------------------------------------- fetching again writes nothing


def snapshot(folder):
    return {f: (os.stat(folder / f).st_mtime_ns, os.stat(folder / f).st_ino, (folder / f).read_bytes())
            for f in files(folder)}


def test_the_same_pdf_fetched_again_writes_nothing(world, docs):
    body = pdf_bytes("again")
    start(world)
    serve(world, "/start", "/doc.pdf")
    serve_pdf(world, "/doc.pdf", body)  # no ETag, so the second fetch is a plain 200 with the same bytes
    crawl(world)
    before = snapshot(docs)
    assert f"{sha(body)}.pdf" in before  # the first run stored it

    world.clock.advance(MONTH_AND_A_DAY)
    crawl(world)
    assert len(pdf_hits(world, "/doc.pdf")) == 2  # the second run really fetched it again
    assert snapshot(docs) == before


def test_a_not_modified_answer_writes_nothing(world, docs):
    body = pdf_bytes("etag")
    etag = '"v1"'

    def answer(rec):
        if rec["headers"].get("if-none-match") == etag:
            return 304, {"ETag": etag}, b""
        return 200, {**PDF_HEADERS, "ETag": etag}, body

    start(world)
    serve(world, "/start", "/doc.pdf")
    world.site.route("/doc.pdf", answer)
    crawl(world)
    before = snapshot(docs)
    assert f"{sha(body)}.pdf" in before

    world.clock.advance(MONTH_AND_A_DAY)
    crawl(world)
    second = pdf_hits(world, "/doc.pdf")[1]
    assert second["headers"].get("if-none-match") == etag  # control: the library file was there, so it stayed conditional
    assert snapshot(docs) == before


def test_a_stored_file_is_never_replaced(world, docs):
    planted, fresh = pdf_bytes("planted"), pdf_bytes("fresh")
    (docs / f"{sha(planted)}.pdf").write_bytes(b"SENTINEL, not the real bytes")
    start(world)
    serve(world, "/start", "/planted.pdf", "/fresh.pdf")
    serve_pdf(world, "/planted.pdf", planted)
    serve_pdf(world, "/fresh.pdf", fresh)
    crawl(world)
    assert (docs / f"{sha(fresh)}.pdf").read_bytes() == fresh  # control: the save path ran for the other PDF
    assert (docs / f"{sha(planted)}.pdf").read_bytes() == b"SENTINEL, not the real bytes"


# ---------------------------------------------------------------- a missing file is re-fetched (D27)


def test_a_known_pdf_whose_file_is_missing_is_fetched_again_without_conditional_headers(world, docs, tmp_path,
                                                                                         monkeypatch):
    body = pdf_bytes("lost folder")
    etag = '"v1"'

    def answer(rec):
        if rec["headers"].get("if-none-match") == etag or rec["headers"].get("if-modified-since"):
            return 304, {"ETag": etag}, b""
        return 200, {**PDF_HEADERS, "ETag": etag, "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"}, body

    start(world)
    serve(world, "/start", "/doc.pdf")
    world.site.route("/doc.pdf", answer)
    crawl(world)
    assert library_pdfs(docs) == [f"{sha(body)}.pdf"]

    lost = use_library(monkeypatch, tmp_path / "docs-lost")  # a folder that has never held this file
    world.clock.advance(MONTH_AND_A_DAY)
    crawl(world)
    second = pdf_hits(world, "/doc.pdf")[1]
    assert "if-none-match" not in second["headers"]
    assert "if-modified-since" not in second["headers"]
    assert library_pdfs(lost) == [f"{sha(body)}.pdf"]
    assert (lost / f"{sha(body)}.pdf").read_bytes() == body


def test_a_known_pdf_with_its_file_present_stays_conditional(world, docs):
    body = pdf_bytes("present")
    etag = '"v1"'

    def answer(rec):
        if rec["headers"].get("if-none-match") == etag:
            return 304, {"ETag": etag}, b""
        return 200, {**PDF_HEADERS, "ETag": etag}, body

    start(world)
    serve(world, "/start", "/doc.pdf")
    world.site.route("/doc.pdf", answer)
    crawl(world)
    world.clock.advance(MONTH_AND_A_DAY)
    crawl(world)
    hits = pdf_hits(world, "/doc.pdf")
    assert len(hits) == 2
    assert "if-none-match" not in hits[0]["headers"]
    assert hits[1]["headers"].get("if-none-match") == etag


# ---------------------------------------------------------------- /x and /x/ are one page (D28)


def test_a_path_with_and_without_a_trailing_slash_is_one_request(world):
    start(world)
    serve(world, "/start", "/product/x", "/product/x/")
    serve(world, "/product/x")
    serve(world, "/product/x/")
    crawl(world)
    both = [p for p in paths(world) if p in ("/product/x", "/product/x/")]
    assert len(both) == 1
    assert one(world, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND url LIKE '%%/product/x%%'",
               (world.brand_id,))[0][0] == 1
    assert "/start" in paths(world)  # control: the crawl ran


def test_a_start_url_with_a_slash_and_a_link_without_one_is_one_request(world):
    start(world, "/product/x/")
    serve(world, "/product/x/", "/product/x")
    serve(world, "/product/x")
    crawl(world)
    assert len([p for p in paths(world) if p in ("/product/x", "/product/x/")]) == 1


def test_the_slash_rule_does_not_merge_different_paths_or_break_the_root(world):
    start(world)
    serve(world, "/start", "/product/x", "/product/x/", "/product/xy", "/product/x/y", "/")
    serve(world, "/product/x")
    serve(world, "/product/x/")
    serve(world, "/product/xy")
    serve(world, "/product/x/y")
    serve(world, "/")
    crawl(world)
    seen = paths(world)
    for p in ("/start", "/product/xy", "/product/x/y", "/"):
        assert seen.count(p) == 1, (p, seen)
    assert len([p for p in seen if p in ("/product/x", "/product/x/")]) == 1
