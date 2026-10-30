"""Parse issues (user, 2026-10-29): documents the code could not handle are kept in crawl.parse_issues with what a fix
needs and the version of the code part that failed. After a code change (a new version) the nightly run takes them
again and marks each fixed or still open; `./crawl issues` writes the open ones as a Markdown fix list for Claude,
kept in control.issue_report for the admin page, which can also set one aside as "won't fix".

Local library in a tmp folder, fake AI, `_testing`, one rolled-back transaction per test."""
import hashlib
import uuid

import psycopg
import pytest

from fakeai import FakeAI
from p3support import make_page, make_pdf, new_brand, q, settle
from test_admin_control import admin_conn  # noqa: F401  (a fixture)
from test_p11b_tagging import tags

GARBLE = " ".join(["xqzt", "brrk", "vvnm", "kjhg", "ttpq", "zzwx"] * 8)


def stored_doc(conn, body):
    """A crawled PDF: its original in the library, its page read (recording what the reader hits), its document."""
    from crawler import library
    from crawler.documents import record_pdf
    library.save(body, "pdf")
    name, brand_id = new_brand(conn)
    page_id = make_page(conn, brand_id, body, is_pdf=True)
    document_id = record_pdf(conn, brand_id, page_id, f"http://p3.test/{uuid.uuid4().hex}.pdf", body)
    return name, page_id, document_id


def issue(conn, body, kind):
    rows = q(conn, "SELECT state, component, detail, seen_version, times_seen, document_id, fixed_version FROM "
                   "parse_issues WHERE content_sha = %s AND kind = %s", (hashlib.sha256(body).hexdigest(), kind))
    return rows[0] if rows else None


def a_pdf(pages=1, *lines):
    tag = uuid.uuid4().hex
    return make_pdf([[f"Manual {tag} page {n + 1}", "Install the unit level on its pad.", *lines] for n in range(pages)])


@pytest.fixture
def short_reader(monkeypatch):
    """The reader as if PDF_MAX_PAGES were 1."""
    import crawler.textextract as te
    real = te.extract_pdf
    monkeypatch.setattr(te, "extract_pdf", lambda data, **kw: real(data, max_pages=1))
    return real


def new_reader(monkeypatch, name="reader-after-fix"):
    import crawler.textextract as te
    monkeypatch.setattr(te, "reader_version", lambda: name)


# ---------------------------------------------------------------- recorded where it happens

def test_an_unreadable_pdf_is_recorded_with_the_readers_own_message(runtime_conn):
    from crawler.issues import scan
    from crawler.textextract import reader_version
    body = b"%PDF-1.4\nthis is not really a pdf " + uuid.uuid4().hex.encode()
    _, _, document_id = stored_doc(runtime_conn, body)
    scan(runtime_conn)
    state, component, detail, seen, times, linked, _ = issue(runtime_conn, body, "unreadable")
    assert (state, component, seen, times, linked) == ("open", "reader", reader_version(), 1, document_id)
    assert detail["error"] == "could not read the PDF" and "pdfchild.py exit" in detail["reader said"]
    assert detail["bytes"] == len(body)


def test_a_pdf_cut_at_the_page_limit_is_recorded(runtime_conn, short_reader):
    body = a_pdf(3)
    stored_doc(runtime_conn, body)
    detail = issue(runtime_conn, body, "cut_at_page_limit")[2]
    assert (detail["pages_in_pdf"], detail["pages_read"]) == (3, 1)


def test_the_same_problem_seen_again_is_one_row_counted_twice(runtime_conn):
    from crawler.textextract import index_page
    body = b"%PDF-1.4\nbroken " + uuid.uuid4().hex.encode()
    _, page_id, _ = stored_doc(runtime_conn, body)
    index_page(runtime_conn, page_id, body, is_pdf=True)
    assert issue(runtime_conn, body, "unreadable")[4] == 2


def test_an_unusable_tagging_answer_is_recorded_with_the_answer(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    body = a_pdf(1, "Model RA15AZ24AJ3CA")
    make_page(runtime_conn, brand_id, body, is_pdf=True)
    settle(runtime_conn, FakeAI(lambda r: "Sorry, I cannot tag this."), name)
    state, component, detail, *_ = issue(runtime_conn, body, "tagging_failed")
    assert (state, component) == ("open", "tagger") and "Sorry" in detail["answers"][0]


# ---------------------------------------------------------------- found by the nightly scan

def test_garbled_text_is_found_and_real_text_is_not():
    from crawler.issues import garbled_pages
    real = "Install the outdoor unit on a level pad. Connect the refrigerant lines and check the charge. " * 4
    spec = "RA15AZ24AJ3CA 24,000 BTU 15.2 SEER2 208/230 V 1 Ph 60 Hz R-454B 3/8 in. 3/4 in. Model Nominal Tons " * 4
    assert garbled_pages([real, spec]) == []
    table = "AFR CFM 365 365 Indoor temperature °F DB 70 75 °F WB 59 63 TC(KBtu) SHC(KBtu) kW " * 6  # Ruud doc 77
    arabic = "ةملاسلا لوح ةماه تامولعلم شرؤم هنأ لىع زمرلا اذه لىع فرعت " * 6  # Ruud doc 375
    glued = ("Safety information Pointsoutanimmediatehazardous Shouldoverheatingoccurorthegassupplyfailstoshutoff "
             "Theheatexchangerhasasmallamount ") * 8  # page 5 of doc 642 scored 0.45
    assert garbled_pages([table, arabic]) == []
    assert [n for n, _, _ in garbled_pages([glued])] == [1]  # Ruud docs 641, 642, 655, 657
    found = garbled_pages([real, GARBLE])
    assert [n for n, _, _ in found] == [2] and found[0][1] == 0 and found[0][2].startswith("xqzt brrk")


def test_a_garbled_document_is_recorded_once_per_reader_version(runtime_conn, monkeypatch):
    from crawler.issues import scan
    body = make_pdf([[f"Manual {uuid.uuid4().hex}", GARBLE]])
    stored_doc(runtime_conn, body)
    scan(runtime_conn)
    scan(runtime_conn)
    state, _, detail, _, times, _, _ = issue(runtime_conn, body, "garbled_text")
    assert state == "open" and times == 1 and detail["garbled_pages"] == [1]
    assert detail["samples"][0]["text"].count("xqzt") >= 1


def test_a_mostly_scanned_document_is_recorded_and_fixed_when_its_text_comes(runtime_conn):
    from crawler.issues import scan
    body = a_pdf(2)
    _, _, document_id = stored_doc(runtime_conn, body)
    q(runtime_conn, "UPDATE documents SET page_count = 10, text_pages = 2 WHERE id = %s", (document_id,))
    scan(runtime_conn)
    assert issue(runtime_conn, body, "mostly_scanned")[0] == "open"
    q(runtime_conn, "UPDATE documents SET text_pages = 9 WHERE id = %s", (document_id,))
    scan(runtime_conn)
    assert issue(runtime_conn, body, "mostly_scanned")[0] == "fixed"


def test_pages_left_out_as_spanish_or_french_do_not_make_a_document_mostly_scanned(runtime_conn):
    # run 4, 2026-10-30: docs 69 and 70, English pages all read, the rest the Spanish and French copies
    from crawler.issues import scan
    body = a_pdf(2)
    _, page_id, document_id = stored_doc(runtime_conn, body)
    for n in range(3, 11):
        q(runtime_conn, "INSERT INTO page_text (page_id, page_number, text_sha, body, has_text, left_out) "
                        "VALUES (%s, %s, %s, '', false, 'language')", (page_id, n, "0" * 64))
    q(runtime_conn, "UPDATE documents SET page_count = 10, text_pages = 2 WHERE id = %s", (document_id,))
    scan(runtime_conn)
    assert issue(runtime_conn, body, "mostly_scanned") is None


# ---------------------------------------------------------------- after a code change

def test_after_a_reader_fix_the_document_is_read_again_and_marked_fixed(runtime_conn, short_reader, monkeypatch):
    from crawler.issues import retry
    import crawler.textextract as te
    body = a_pdf(3)
    _, _, document_id = stored_doc(runtime_conn, body)
    monkeypatch.setattr(te, "extract_pdf", short_reader)  # the fix: the whole PDF is read
    new_reader(monkeypatch)
    assert retry(runtime_conn) == {"retried": 1, "fixed": 1}
    state, *_, fixed = issue(runtime_conn, body, "cut_at_page_limit")
    assert (state, fixed) == ("fixed", "reader-after-fix")
    assert q(runtime_conn, "SELECT page_count FROM documents WHERE id = %s", (document_id,))[0][0] == 3


def test_a_problem_the_fix_did_not_solve_stays_open_under_the_new_version(runtime_conn, monkeypatch):
    from crawler.issues import retry
    body = b"%PDF-1.4\nstill broken " + uuid.uuid4().hex.encode()
    stored_doc(runtime_conn, body)
    new_reader(monkeypatch)
    assert retry(runtime_conn)["fixed"] == 0
    state, _, _, seen, times, _, _ = issue(runtime_conn, body, "unreadable")
    assert (state, seen, times) == ("open", "reader-after-fix", 2)


def test_nothing_is_retried_while_the_code_is_unchanged(runtime_conn):
    from crawler.issues import retry
    stored_doc(runtime_conn, b"%PDF-1.4\nbroken " + uuid.uuid4().hex.encode())
    q(runtime_conn, "UPDATE parse_issues SET state = 'wont_fix' WHERE state = 'open' AND seen_version <> %s",
      (__import__("crawler.textextract").textextract.reader_version(),))  # older rows in `_testing` stay out of it
    assert retry(runtime_conn) == {"retried": 0, "fixed": 0}


def test_after_a_tagger_fix_the_document_is_tagged_again_and_marked_fixed(runtime_conn, monkeypatch):
    import crawler.extract
    from crawler.issues import retry
    name, brand_id = new_brand(runtime_conn)
    body = a_pdf(1, "Model RA15AZ24AJ3CA")
    make_page(runtime_conn, brand_id, body, is_pdf=True)
    settle(runtime_conn, FakeAI(lambda r: "Sorry, I cannot tag this."), name)
    monkeypatch.setattr(crawler.extract, "EXTRACTOR_VERSION", "tagger-after-fix")
    retry(runtime_conn)
    assert issue(runtime_conn, body, "tagging_failed")[0] == "retrying"
    settle(runtime_conn, FakeAI(lambda r: tags()), name)
    retry(runtime_conn)
    state, *_, fixed = issue(runtime_conn, body, "tagging_failed")
    assert (state, fixed) == ("fixed", "tagger-after-fix")


def test_an_upload_the_reader_failed_on_is_queued_again_after_a_fix(runtime_conn, monkeypatch):
    import os
    from pathlib import Path
    from crawler.issues import retry
    from crawler.uploads import process_uploads
    body = b"%PDF-1.4\nupload broken " + uuid.uuid4().hex.encode()
    key = f"inbox/{uuid.uuid4().hex}/rep.pdf"
    path = Path(os.environ["DOCS_DIR"], key)
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    upload = q(runtime_conn, "INSERT INTO control.uploads (s3_key, file_name) VALUES (%s, 'rep.pdf') RETURNING id",
               (key,))[0][0]
    process_uploads(runtime_conn)
    assert issue(runtime_conn, body, "unreadable")[2]["upload_id"] == upload
    new_reader(monkeypatch)
    retry(runtime_conn)
    assert q(runtime_conn, "SELECT state FROM control.uploads WHERE id = %s", (upload,))[0][0] == "queued"


# ---------------------------------------------------------------- the fix list and the admin page

def test_the_fix_list_is_markdown_grouped_by_kind_and_kept_for_the_admin_page(runtime_conn, short_reader):
    from crawler.issues import report
    body = a_pdf(3)
    _, _, document_id = stored_doc(runtime_conn, body)
    text = report(runtime_conn)
    sha = hashlib.sha256(body).hexdigest()
    assert text.startswith("# Crawler parse issues")
    assert "## cut_at_page_limit (" in text and "PDF_MAX_PAGES" in text
    assert f"`{sha}.pdf`" in text and f"document {document_id}" in text and "pages in pdf: 3" in text
    assert "test-first" in text
    stored = q(runtime_conn, "SELECT markdown, open_issues FROM control.issue_report WHERE name = 'latest'")[0]
    assert stored[0] == text and stored[1] >= 1


def test_the_admin_page_reads_issues_and_can_only_set_one_aside(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("SELECT * FROM control.parse_issues LIMIT 1")
        cur.execute("SELECT * FROM control.issue_report LIMIT 1")
        cur.execute("UPDATE control.parse_issue_marks SET state = 'wont_fix', note = 'probe' WHERE false")
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg.errors.UndefinedColumn):  # only state and note are there to change
            cur.execute("UPDATE control.parse_issue_marks SET detail = '{}' WHERE false")
        cur.execute("ROLLBACK TO SAVEPOINT s")
    from test_p1_users import refused
    assert refused(admin_conn, "DELETE FROM control.parse_issue_marks WHERE false")
    from test_p1_users import privileges
    assert privileges(admin_conn, "eq_admin", "control.parse_issues") == {"SELECT"}


def test_a_problem_set_aside_is_never_retried(runtime_conn, monkeypatch):
    from crawler.issues import retry
    body = b"%PDF-1.4\nbroken " + uuid.uuid4().hex.encode()
    stored_doc(runtime_conn, body)
    q(runtime_conn, "UPDATE parse_issues SET state = 'wont_fix' WHERE content_sha = %s", (hashlib.sha256(body).hexdigest(),))
    new_reader(monkeypatch)
    retry(runtime_conn)
    assert issue(runtime_conn, body, "unreadable")[4] == 1


def test_issues_is_a_command():
    from crawler.__main__ import build_parser
    assert build_parser().parse_args(["issues"]).command == "issues"


# ---------------------------------------------------------------- review fixes (2026-10-30)

def test_a_document_that_could_not_be_read_again_is_not_marked_fixed(runtime_conn, short_reader, monkeypatch):
    # its original is gone from the library: nothing was read, so nothing can be called fixed
    import os
    from crawler.issues import retry
    import crawler.textextract as te
    body = a_pdf(3)
    stored_doc(runtime_conn, body)
    os.remove(os.path.join(os.environ["DOCS_DIR"], hashlib.sha256(body).hexdigest() + ".pdf"))
    monkeypatch.setattr(te, "extract_pdf", short_reader)
    new_reader(monkeypatch)
    assert retry(runtime_conn)["fixed"] == 0
    assert issue(runtime_conn, body, "cut_at_page_limit")[0] == "open"


def test_a_requeued_upload_that_is_then_added_marks_its_issue_fixed(runtime_conn, monkeypatch):
    import os
    from pathlib import Path
    import crawler.textextract as te
    from crawler.issues import retry
    from crawler.uploads import process_uploads
    good = a_pdf(1)
    key = f"inbox/{uuid.uuid4().hex}/rep.pdf"
    path = Path(os.environ["DOCS_DIR"], key)
    path.parent.mkdir(parents=True)
    path.write_bytes(good)
    real = te.extract_pdf

    def broken(data, **kw):
        raise te.ExtractError("could not read the PDF", "pdfchild.py exit 1: boom")
    monkeypatch.setattr(te, "extract_pdf", broken)
    import crawler.uploads
    monkeypatch.setattr(crawler.uploads, "extract_pdf", broken)
    upload = q(runtime_conn, "INSERT INTO control.uploads (s3_key, file_name) VALUES (%s, 'rep.pdf') RETURNING id",
               (key,))[0][0]
    process_uploads(runtime_conn)
    monkeypatch.setattr(crawler.uploads, "extract_pdf", real)  # the fix
    new_reader(monkeypatch)
    retry(runtime_conn)
    process_uploads(runtime_conn)
    assert q(runtime_conn, "SELECT state FROM control.uploads WHERE id = %s", (upload,))[0][0] == "added"
    assert issue(runtime_conn, good, "unreadable")[0] == "fixed"


def test_a_reader_issue_with_nothing_to_read_again_is_not_counted_as_retried(runtime_conn, monkeypatch):
    from crawler.issues import record, retry
    q(runtime_conn, "UPDATE parse_issues SET state = 'wont_fix' WHERE state = 'open'")  # older rows in `_testing`
    record(runtime_conn, "f" * 64, "unreadable", {"error": "x"})  # no document, no upload
    new_reader(monkeypatch)
    assert retry(runtime_conn)["retried"] == 0
