"""P19 (D84): documents uploaded by hand from the admin page. The page puts the file in inbox/ and queues a
control.uploads row; the next run (or `./crawl uploads`) checks it is a readable PDF, stores the original once under its
sha256, reads its text and makes it a document like a crawled PDF, then writes the outcome back: added, already had it
(the same file is stored), or rejected with the reason. The brand picked on the page wins; with none picked, the brand
the text names most is used (user, 2026-10-29); with none named, it has no brand and Breeze still uses it (D82).

Local library in a tmp folder (DOCS_DIR, inbox/ inside it), fake AI, `_testing`, one rolled-back transaction per test."""
import hashlib
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from p11support import world  # noqa: F401  (world is a fixture)
from p3support import make_pdf, q
from test_admin_control import admin_conn  # noqa: F401  (a fixture)
from test_auto_run import auto


def a_pdf(*lines):
    return make_pdf([[f"Upload {uuid.uuid4().hex}", *lines]])


def queue(conn, body, name="manual.pdf", brand=None, in_inbox=True):
    key = f"inbox/{uuid.uuid4().hex}/{name}"
    if in_inbox:
        path = Path(os.environ["DOCS_DIR"], key)
        path.parent.mkdir(parents=True)
        path.write_bytes(body)
    brand_id = q(conn, "SELECT id FROM control.brands WHERE name = %s", (brand,))[0][0] if brand else None
    return q(conn, "INSERT INTO control.uploads (s3_key, file_name, brand_id, uploaded_by) VALUES (%s, %s, %s, 'admin') "
                   "RETURNING id", (key, name, brand_id))[0][0]


def process(conn):
    from crawler.uploads import process_uploads
    return process_uploads(conn)


def outcome(conn, upload_id):
    return q(conn, "SELECT state, reason, document_id, manufacturer_id FROM control.uploads WHERE id = %s",
             (upload_id,))[0]


def maker_of(conn, brand_id):
    from crawler.store import brand_manufacturer
    return brand_manufacturer(conn, brand_id)


def test_a_queued_pdf_becomes_a_document_of_the_brand_picked_on_the_page(world):
    from crawler import library
    body = a_pdf("Model RA15AZ24AJ3CA", "Installation instructions")
    upload = queue(world.conn, body, brand=world.brand)
    assert process(world.conn)["added"] == 1
    state, reason, document_id, maker = outcome(world.conn, upload)
    assert state == "added" and reason is None and document_id is not None
    assert maker == maker_of(world.conn, world.brand_id)
    sha = hashlib.sha256(body).hexdigest()
    assert q(world.conn, "SELECT content_sha, manufacturer_id FROM documents WHERE id = %s", (document_id,))[0] == (sha, maker)
    assert library.has_file(sha)
    assert q(world.conn, "SELECT COUNT(*) FROM page_text t JOIN control.uploads u ON u.page_id = t.page_id "
                         "WHERE u.id = %s AND t.has_text", (upload,))[0][0] == 1
    assert q(world.conn, "SELECT manufacturer_id FROM document_brands WHERE document_id = %s", (document_id,)) == ((maker,),)
    assert q(world.conn, "SELECT processed_at IS NOT NULL FROM control.uploads WHERE id = %s", (upload,))[0][0]


def test_the_same_file_uploaded_twice_is_already_had(world):
    body = a_pdf("Model RA15AZ24AJ3CA")
    first, second = queue(world.conn, body, brand=world.brand), queue(world.conn, body, name="again.pdf")
    summary = process(world.conn)
    assert (summary["added"], summary["already_had"]) == (1, 1)
    assert outcome(world.conn, second)[0] == "already_had"
    assert outcome(world.conn, second)[2] == outcome(world.conn, first)[2]


def test_a_brand_picked_for_a_file_already_stored_is_added_to_it(world):
    body = a_pdf("Model RA15AZ24AJ3CA")
    queue(world.conn, body)  # no brand, none in the text
    process(world.conn)
    again = queue(world.conn, body, brand=world.brand)
    process(world.conn)
    document_id = outcome(world.conn, again)[2]
    assert q(world.conn, "SELECT manufacturer_id FROM document_brands WHERE document_id = %s",
             (document_id,)) == ((maker_of(world.conn, world.brand_id),),)


@pytest.mark.parametrize("body, in_inbox, words", [
    (b"Dear rep, here is the manual you asked for.", True, "not a PDF"),
    (b"%PDF-1.4\nnot really a pdf at all", True, "could not be read"),
    (None, False, "not in the inbox"),
])
def test_a_file_that_is_not_a_readable_pdf_is_rejected_with_the_reason(world, body, in_inbox, words):
    upload = queue(world.conn, body or b"", brand=world.brand, in_inbox=in_inbox)
    assert process(world.conn)["rejected"] == 1
    state, reason, document_id, _ = outcome(world.conn, upload)
    assert state == "rejected" and words in reason and document_id is None


def test_with_no_brand_picked_the_brand_the_text_names_most_is_used(world):
    from crawler.brands import add_brand
    word = "Zorbex" + uuid.uuid4().hex[:6]
    other = add_brand(world.conn, word)
    body = a_pdf(f"{word} split system heat pump", f"Warranty: {word} warrants this unit",
                 f"Call {word} service", "Replaces a Carrier unit")
    upload = queue(world.conn, body)
    process(world.conn)
    state, _, document_id, maker = outcome(world.conn, upload)
    assert state == "added" and maker == maker_of(world.conn, other)
    assert q(world.conn, "SELECT p.brand_id FROM pages p JOIN control.uploads u ON u.page_id = p.id WHERE u.id = %s",
             (upload,))[0][0] == other


def test_with_no_brand_picked_or_named_it_is_kept_without_one(world):
    upload = queue(world.conn, a_pdf("Generic thermostat wiring guide", "Model TX1000"))
    process(world.conn)
    state, _, document_id, maker = outcome(world.conn, upload)
    assert state == "added" and maker is None
    assert q(world.conn, "SELECT manufacturer_id FROM documents WHERE id = %s", (document_id,))[0][0] is None
    assert q(world.conn, "SELECT COUNT(*) FROM document_brands WHERE document_id = %s", (document_id,))[0][0] == 0


def discovered(world):
    """The world brand has no start address: mark it set up, so the run does not try to discover it."""
    q(world.conn, "UPDATE brands SET discovered_at = now() WHERE id = %s", (world.brand_id,))


def test_an_upload_is_tagged_and_live_after_the_next_run_and_never_fetched(world):
    discovered(world)
    upload = queue(world.conn, a_pdf("Model RA15AZ24AJ3CA", "Installation instructions"), brand=world.brand)
    summary = auto(world)
    assert summary["uploads"] == {"added": 1, "already_had": 0, "rejected": 0}
    assert "Uploads: added 1" in q(world.conn, "SELECT report FROM runs WHERE id = %s", (summary["run_id"],))[0][0]
    document_id = outcome(world.conn, upload)[2]
    assert q(world.conn, "SELECT state FROM documents WHERE id = %s", (document_id,))[0][0] == "live"
    assert q(world.conn, "SELECT p.next_check_at FROM pages p JOIN control.uploads u ON u.page_id = p.id "
                         "WHERE u.id = %s", (upload,))[0][0] is None
    auto(world)
    assert world.site.page_hits() == []  # nothing was ever asked of a web server for it


def test_an_uploaded_manual_without_a_brand_is_tagged_too(world):
    discovered(world)
    upload = queue(world.conn, a_pdf("Generic thermostat wiring guide", "Model TX1000"))
    auto(world)
    document_id = outcome(world.conn, upload)[2]
    assert q(world.conn, "SELECT state FROM documents WHERE id = %s", (document_id,))[0][0] == "live"


def test_the_admin_page_can_queue_an_inbox_file_and_nothing_else(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.uploads (s3_key, file_name, note, uploaded_by) VALUES "
                    "('inbox/2026/rep-manual.pdf', 'rep-manual.pdf', 'from the Ruud rep', 'cesar') RETURNING state")
        assert cur.fetchone()[0] == "queued"
    for key in ("docs/abc.pdf", "inbox/../abc.pdf", "inbox/", "abc.pdf"):
        with admin_conn.cursor() as cur:
            cur.execute("SAVEPOINT s")
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO control.uploads (s3_key, file_name) VALUES (%s, 'x.pdf')", (key,))
            cur.execute("ROLLBACK TO SAVEPOINT s")
    from test_p1_users import refused
    assert refused(admin_conn, "UPDATE control.uploads SET state = 'added' WHERE false")
    assert refused(admin_conn, "DELETE FROM control.uploads WHERE false")


def test_uploads_is_a_command():
    from crawler.__main__ import build_parser
    assert build_parser().parse_args(["uploads"]).command == "uploads"


def test_an_upload_the_crawler_fails_on_is_rejected_and_the_rest_go_on(world, monkeypatch):
    # review 2026-10-30: one upload failing in an unexpected way blocked every upload after it, every night
    import crawler.uploads
    real = crawler.uploads.index_page
    calls = []

    def flaky(conn, page_id, body, **kw):
        calls.append(page_id)
        if len(calls) == 1:
            raise RuntimeError("a database error on this file")
        return real(conn, page_id, body, **kw)
    monkeypatch.setattr(crawler.uploads, "index_page", flaky)
    first = queue(world.conn, a_pdf("Model RA15AZ24AJ3CA"), brand=world.brand)
    second = queue(world.conn, a_pdf("Model RA15AZ36AJ3CA"), brand=world.brand)
    assert process(world.conn) == {"added": 1, "already_had": 0, "rejected": 1}
    state, reason, document_id, _ = outcome(world.conn, first)
    assert state == "rejected" and "RuntimeError" in reason and document_id is None
    assert outcome(world.conn, second)[0] == "added"


def test_an_upload_for_a_paused_brand_is_still_tagged(world):
    discovered(world)
    upload = queue(world.conn, a_pdf("Model RA15AZ24AJ3CA", "Installation instructions"), brand=world.brand)
    from crawler.brands import stop
    stop(world.conn, world.brand)
    auto(world)
    document_id = outcome(world.conn, upload)[2]
    assert q(world.conn, "SELECT state FROM documents WHERE id = %s", (document_id,))[0][0] == "live"
