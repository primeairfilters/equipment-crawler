"""Reading stored PDFs again (2026-10-27): the first Ruud run stored 16 big manuals with 0 pages, because the PDF reader
ran out of memory. `reread` reads the kept originals again, fetching nothing, and `status` says how many are unread.
"""
import hashlib

from p11support import crawl, serve, serve_pdf, start, world  # noqa: F401  (world is a fixture)
from p3support import make_pdf


def unreadable_crawl(world, monkeypatch, *paths):
    """Crawl PDFs while the reader fails, the way a big manual failed on the memory limit."""
    import crawler.textextract
    from crawler.textextract import ExtractError

    def fail(*args, **kwargs):
        raise ExtractError("could not read the PDF")

    real = crawler.textextract.extract_pdf
    monkeypatch.setattr(crawler.textextract, "extract_pdf", fail)
    start(world)
    serve(world, "/start", *paths)
    body = make_pdf([["Service manual for RA15AZ24AJ3CA", "Charging the system"], ["Wiring diagram page two"]])
    for path in paths:
        serve_pdf(world, path, body)
    crawl(world)
    monkeypatch.setattr(crawler.textextract, "extract_pdf", real)
    return hashlib.sha256(body).hexdigest()


def document(world, sha):
    return world.one("SELECT page_count, text_pages, needs_ocr FROM documents WHERE content_sha = %s", (sha,))


def test_a_pdf_the_reader_failed_on_is_read_again_from_the_kept_original(world, monkeypatch):
    from crawler.documents import reread
    sha = unreadable_crawl(world, monkeypatch, "/manual.pdf")
    assert document(world, sha) == ((0, 0, False),)
    hits = len(world.site.hits())
    assert reread(world.conn) == {"read": 1, "unreadable": 0}
    assert document(world, sha) == ((2, 2, False),)
    assert world.one("SELECT extraction_status FROM pages WHERE url = %s",
                     (world.url("ruud.test", "/manual.pdf"),)) == (("waiting",),)  # now goes to the AI for tags
    assert len(world.site.hits()) == hits  # nothing fetched


def test_the_same_pdf_at_two_addresses_gets_its_text_at_both(world, monkeypatch):
    from crawler.documents import reread
    sha = unreadable_crawl(world, monkeypatch, "/a.pdf", "/b.pdf")
    reread(world.conn)
    assert world.one("SELECT COUNT(DISTINCT page_id) FROM page_text pt JOIN document_urls du USING (page_id) "
                     "JOIN documents d ON d.id = du.document_id WHERE d.content_sha = %s", (sha,)) == ((2,),)


def test_a_document_already_read_is_left_alone(world, monkeypatch):
    from crawler.documents import reread
    start(world)
    serve(world, "/start", "/ok.pdf")
    serve_pdf(world, "/ok.pdf")
    crawl(world)
    assert reread(world.conn) == {"read": 0, "unreadable": 0}


def test_status_says_how_many_documents_could_not_be_read(world, monkeypatch):
    from crawler.brands import status
    unreadable_crawl(world, monkeypatch, "/manual.pdf")
    row = next(r for r in status(world.conn) if r["brand"] == world.brand)
    assert row["unreadable"] == 1


def test_a_document_needing_ocr_is_read_again_so_a_better_reader_can_recover_pages(world, monkeypatch):
    import crawler.textextract
    from crawler.documents import reread
    from crawler.textextract import Extracted

    real = crawler.textextract.extract_pdf
    monkeypatch.setattr(crawler.textextract, "extract_pdf", lambda data, **kw: Extracted(pages=["", ""]))  # an older reader
    start(world)
    serve(world, "/start", "/iom.pdf")
    body = make_pdf([["Installation manual for RA15AZ24AJ3CA", "Charging"], ["Wiring diagram page two"]])
    serve_pdf(world, "/iom.pdf", body)
    crawl(world)
    sha = hashlib.sha256(body).hexdigest()
    assert document(world, sha) == ((2, 0, True),)
    monkeypatch.setattr(crawler.textextract, "extract_pdf", real)
    assert reread(world.conn) == {"read": 1, "unreadable": 0}
    assert document(world, sha) == ((2, 2, False),)


def test_a_document_cut_at_an_old_page_limit_is_read_whole_when_named(world):
    # 2026-10-29: the page limit went from 200 to 300; docs 799, 800 and 604 were stored cut at 200
    import crawler.textextract
    from crawler.documents import reread
    defaults = crawler.textextract.extract_pdf.__defaults__
    crawler.textextract.extract_pdf.__defaults__ = (1,) + defaults[1:]  # stored cut at one page
    start(world)
    serve(world, "/start", "/big.pdf")
    body = make_pdf([["Service manual for RA15AZ24AJ3CA", "Charging the system"], ["Wiring diagram page two"]])
    serve_pdf(world, "/big.pdf", body)
    try:
        crawl(world)
    finally:
        crawler.textextract.extract_pdf.__defaults__ = defaults
    sha = hashlib.sha256(body).hexdigest()
    assert document(world, sha) == ((1, 1, False),)
    assert reread(world.conn) == {"read": 0, "unreadable": 0}  # nothing marks it as cut
    doc = world.one("SELECT id FROM documents WHERE content_sha = %s", (sha,))[0][0]
    assert reread(world.conn, documents=[doc]) == {"read": 1, "unreadable": 0}
    assert document(world, sha) == ((2, 2, False),)


def test_the_reread_command_takes_documents_by_id():
    from crawler.__main__ import build_parser
    args = build_parser().parse_args(["reread", "--document", "799", "--document", "800"])
    assert args.document == [799, 800]
