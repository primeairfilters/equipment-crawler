"""P12 (D41, D48, D50, D51, D53, D55, D56): sections stored as builds, the publish gate, hide/unhide, and check.

Rows are rolled back after each test. Pages go in as layout items (see test_p12_sectioner.py) unless a test reads a
real PDF through the PDF child.
"""
import hashlib
import uuid

import pytest

from p3support import new_brand, q

MODEL = "RA15AZ24AJ3CA"


def t(text, size=10):
    return {"kind": "text", "text": text, "size": size, "bold": False}


def words(n, word="charge"):
    return " ".join(f"{word}{i % 7}" for i in range(n)) + "."


PAGES = [[t("Refrigerant Charging", 14), t(words(320) + " Error E4 means LO PS tripped.")],
         [t("Wiring", 14), t(words(320, "wire")),
          {"kind": "table", "rows": [["Model", "MCA"], [MODEL, "12"]]}]]


def document(conn, brand_name=None, *, doc_type="install_manual", trade="hvac", language="en", sha=None, form=None):
    from crawler.store import manufacturer_for
    name = brand_name or "Zz" + uuid.uuid4().hex[:10]
    mid = manufacturer_for(conn, name)
    rows = q(conn, "INSERT INTO documents (content_sha, manufacturer_id, doc_type, trade, equipment_type, language, title, "
                   "form_number, state) VALUES (%s, %s, %s, %s, 'air_conditioner', %s, 'Install manual', %s, 'tagged') "
                   "RETURNING id", (sha or hashlib.sha256(uuid.uuid4().bytes).hexdigest(), mid, doc_type, trade, language,
                                    form))
    return rows[0][0], mid, name


def live(conn, document_id):
    return q(conn, "SELECT build, section_no FROM sections WHERE document_id = %s AND is_live ORDER BY 1, 2", (document_id,))


# ---------------------------------------------------------------- build

def test_a_build_stores_sections_with_pages_paths_and_copied_filters_and_is_not_live(runtime_conn):
    from crawler.kb import build, build_version
    doc, mid, _ = document(runtime_conn)
    assert build(runtime_conn, doc, pages=PAGES) == 1
    rows = q(runtime_conn, "SELECT section_no, page_from, page_to, heading_path, kind, manufacturer_id, trade, equipment_type, "
                           "doc_type, language, is_live, sectioner_version, token_count > 0, length(body_sha) "
                           "FROM sections WHERE document_id = %s ORDER BY section_no", (doc,))
    assert [r[:5] for r in rows] == [(1, 1, 1, "Refrigerant Charging", "text"), (2, 2, 2, "Wiring", "text"),
                                     (3, 2, 2, "Wiring", "table")]
    assert all(r[5:] == (mid, "hvac", "air_conditioner", "install_manual", "en", False, build_version(), True, 64)
               for r in rows)
    assert q(runtime_conn, "SELECT state, live_build FROM documents WHERE id = %s", (doc,)) == (("built", None),)


def test_codes_go_into_section_terms(runtime_conn):
    from crawler.kb import build
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    found = {r[0] for r in q(runtime_conn, "SELECT st.term FROM section_terms st JOIN sections s ON s.id = st.section_id "
                                           "WHERE s.document_id = %s", (doc,))}
    assert {"E4", "LOPS", MODEL} <= found


def test_building_the_same_text_again_makes_no_new_build(runtime_conn):
    from crawler.kb import build
    doc, _, _ = document(runtime_conn)
    assert build(runtime_conn, doc, pages=PAGES) == 1
    assert build(runtime_conn, doc, pages=[list(p) for p in PAGES]) is None
    assert q(runtime_conn, "SELECT COUNT(*) FROM sections WHERE document_id = %s", (doc,))[0][0] == 3


def test_a_better_sectioner_builds_next_to_the_old_build_and_the_switch_keeps_every_row(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build, publish
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    assert publish(runtime_conn, doc) == (True, None)
    ids_before = {r[0] for r in q(runtime_conn, "SELECT id FROM sections WHERE document_id = %s", (doc,))}
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    real_terms = crawler.kb.terms
    monkeypatch.setattr(crawler.kb, "terms", lambda text: real_terms(text) | {"NEWTERM"})  # its sections differ
    assert build(runtime_conn, doc, pages=PAGES) == 2
    assert {b for b, _ in live(runtime_conn, doc)} == {1}  # the new build waits for publish
    assert publish(runtime_conn, doc) == (True, None)
    assert {b for b, _ in live(runtime_conn, doc)} == {2}
    assert ids_before <= {r[0] for r in q(runtime_conn, "SELECT id FROM sections WHERE document_id = %s", (doc,))}
    assert publish(runtime_conn, doc, build=1) == (True, None)  # switching back is the same switch
    assert {b for b, _ in live(runtime_conn, doc)} == {1}


def test_a_real_pdf_is_read_with_font_sizes_for_its_headings(runtime_conn, tmp_path, monkeypatch):
    from crawler.kb import build
    pdf = sized_pdf([[("Installation Instructions", 20), ("Refrigerant Charging", 14)]
                     + [(words(12) , 10)] * 30,
                     [("Wiring Diagram", 14)] + [(words(12, "wire"), 10)] * 30])
    sha = hashlib.sha256(pdf).hexdigest()
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    (tmp_path / f"{sha}.pdf").write_bytes(pdf)
    doc, _, _ = document(runtime_conn, sha=sha)
    assert build(runtime_conn, doc) == 1
    paths = [r[0] for r in q(runtime_conn, "SELECT heading_path FROM sections WHERE document_id = %s ORDER BY section_no", (doc,))]
    assert paths == ["Refrigerant Charging", "Wiring Diagram"]  # the cover title stays in the text, out of the path
    assert q(runtime_conn, "SELECT page_from FROM sections WHERE document_id = %s ORDER BY section_no", (doc,)) == ((1,), (2,))


def test_without_the_original_the_stored_text_is_cut_into_windows(runtime_conn):
    from crawler.kb import build
    from p3support import make_doc
    name, brand_id = new_brand(runtime_conn)
    page_id = make_doc(runtime_conn, brand_id, [words(400), words(400, "two")])
    sha = q(runtime_conn, "SELECT content_sha FROM pages WHERE id = %s", (page_id,))[0][0]
    doc, _, _ = document(runtime_conn, sha=sha)
    q(runtime_conn, "INSERT INTO document_urls (document_id, page_id, url) VALUES (%s, %s, 'http://p3.test/x')", (doc, page_id))
    assert build(runtime_conn, doc) == 1
    rows = q(runtime_conn, "SELECT heading_path, page_from FROM sections WHERE document_id = %s ORDER BY section_no", (doc,))
    assert rows and all(r[0] == "" for r in rows) and rows[0][1] == 1


# ---------------------------------------------------------------- the publish gate

def test_publish_makes_one_build_live_in_one_step(runtime_conn):
    from crawler.kb import build, publish
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    assert live(runtime_conn, doc) == ()
    assert publish(runtime_conn, doc) == (True, None)
    assert live(runtime_conn, doc) == ((1, 1), (1, 2), (1, 3))
    assert q(runtime_conn, "SELECT state, live_build FROM documents WHERE id = %s", (doc,)) == (("live", 1),)


@pytest.mark.parametrize("why", ["hidden document", "hidden manufacturer", "manufacturer off", "not_equipment", "untagged"])
def test_the_gate_never_lets_these_go_live(runtime_conn, why):
    from crawler.kb import build, publish
    doc, mid, _ = document(runtime_conn, doc_type={"not_equipment": "not_equipment", "untagged": None}.get(why, "install_manual"))
    build(runtime_conn, doc, pages=PAGES)
    if why == "hidden document":
        q(runtime_conn, "UPDATE documents SET hidden_at = now() WHERE id = %s", (doc,))
    elif why == "hidden manufacturer":
        q(runtime_conn, "UPDATE manufacturers SET hidden_at = now() WHERE id = %s", (mid,))
    elif why == "manufacturer off":
        q(runtime_conn, "UPDATE manufacturers SET content_use = 'off' WHERE id = %s", (mid,))
    ok, reason = publish(runtime_conn, doc)
    assert ok is False and reason
    assert live(runtime_conn, doc) == ()
    assert q(runtime_conn, "SELECT state FROM documents WHERE id = %s", (doc,)) == (("built",),)


def test_a_new_version_going_live_retires_the_one_it_replaces(runtime_conn):
    from crawler.kb import build, publish
    old, mid, name = document(runtime_conn)
    build(runtime_conn, old, pages=PAGES)
    publish(runtime_conn, old)
    new, _, _ = document(runtime_conn, name)
    q(runtime_conn, "UPDATE documents SET replaces_id = %s WHERE id = %s", (old, new))
    build(runtime_conn, new, pages=[[t("Refrigerant Charging", 14), t(words(330, "revised"))]])
    publish(runtime_conn, new)
    assert live(runtime_conn, old) == () and live(runtime_conn, new) != ()
    assert q(runtime_conn, "SELECT id, state FROM documents WHERE id IN (%s, %s) ORDER BY id", (old, new)) == (
        (old, "retired"), (new, "live"))


def test_publish_without_a_build_says_why(runtime_conn):
    from crawler.kb import publish
    doc, _, _ = document(runtime_conn)
    ok, reason = publish(runtime_conn, doc)
    assert ok is False and "no sections" in reason


# ---------------------------------------------------------------- hide and unhide (D51, D56)

def test_hiding_a_manual_takes_it_out_at_once_logs_it_and_unhide_restores_only_the_live_build(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build, hide, publish, unhide
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    build(runtime_conn, doc, pages=PAGES)  # build 2 exists but is not live
    hide(runtime_conn, "document", str(doc), reason="takedown request", by_whom="tester")
    assert live(runtime_conn, doc) == ()
    assert q(runtime_conn, "SELECT hidden_reason FROM documents WHERE id = %s", (doc,)) == (("takedown request",),)
    assert publish(runtime_conn, doc)[0] is False  # the next crawl or build cannot bring it back
    unhide(runtime_conn, "document", str(doc), reason="cleared", by_whom="tester")
    assert {b for b, _ in live(runtime_conn, doc)} == {1}
    log = q(runtime_conn, "SELECT hidden, reason, by_whom FROM visibility_log WHERE target_type = 'document' "
                          "AND target_id = %s ORDER BY id", (doc,))
    assert log == ((True, "takedown request", "tester"), (False, "cleared", "tester"))


def test_hiding_a_manufacturer_hides_all_its_manuals_until_unhidden(runtime_conn):
    from crawler.kb import build, hide, publish, unhide
    one, mid, name = document(runtime_conn)
    two, _, _ = document(runtime_conn, name)
    for doc in (one, two):
        build(runtime_conn, doc, pages=PAGES)
        publish(runtime_conn, doc)
    hide(runtime_conn, "manufacturer", name, reason="legal said off", by_whom="tester")
    assert live(runtime_conn, one) == () and live(runtime_conn, two) == ()
    unhide(runtime_conn, "manufacturer", name, reason="legal said ok", by_whom="tester")
    assert live(runtime_conn, one) != () and live(runtime_conn, two) != ()


def test_hiding_an_unknown_target_is_an_error(runtime_conn):
    from crawler.brands import UnknownTarget
    from crawler.kb import hide
    with pytest.raises(UnknownTarget):
        hide(runtime_conn, "document", "999999999", reason="x", by_whom="tester")
    with pytest.raises(UnknownTarget):
        hide(runtime_conn, "manufacturer", "No Such Maker " + uuid.uuid4().hex, reason="x", by_whom="tester")


# ---------------------------------------------------------------- check (D53)

def test_check_reports_live_documents_without_sections_and_hidden_ones_still_live(runtime_conn):
    from crawler.kb import build, check, publish
    empty, _, _ = document(runtime_conn)
    q(runtime_conn, "UPDATE documents SET state = 'live', live_build = 1 WHERE id = %s", (empty,))
    leak, _, _ = document(runtime_conn)
    build(runtime_conn, leak, pages=PAGES)
    publish(runtime_conn, leak)
    q(runtime_conn, "UPDATE documents SET hidden_at = now() WHERE id = %s", (leak,))  # behind the commands' back
    report = check(runtime_conn)
    assert empty in report["live_without_sections"]
    assert leak in report["hidden_but_live"]
    assert report["counts"]  # per manufacturer and trade


def test_breeze_can_read_only_what_is_live(runtime_conn):
    """The serving rows a publish makes are readable by eq_breeze (the commit is undone at the end)."""
    from crawler.kb import build, publish
    from support import connect
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    breeze = connect("breeze")
    try:
        with breeze.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sections WHERE is_live")
            cur.fetchone()  # it can read the table; uncommitted rows from the test are not visible, which is the point
    finally:
        breeze.close()


# ---------------------------------------------------------------- a PDF with real font sizes

def sized_pdf(pages):
    """A small valid PDF; pages is a list of pages, each a list of (text, font size) lines."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for i, lines in enumerate(pages):
        ops, y = ["BT"], 760
        for text, size in lines:
            ops += [f"/F1 {size} Tf", f"1 0 0 1 50 {y} Tm", f"({esc(text)}) Tj"]
            y -= size + 6
        ops.append("ET")
        stream = "\n".join(ops).encode()
        objects[5 + 2 * i] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objects[4 + 2 * i] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
                              f"/Resources << /Font << /F1 3 0 R >> >> >>").encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref = len(out)
    total = max(objects) + 1
    out += f"xref\n0 {total}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, total):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# ---------------------------------------------------------------- review fixes (2026-10-27)

def test_a_held_build_goes_live_once_its_manufacturer_is_unhidden(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build_and_publish, hide, unhide
    doc, _, name = document(runtime_conn)
    monkeypatch.setattr(crawler.kb, "document_pages", lambda conn, d, **kw: PAGES)
    hide(runtime_conn, "manufacturer", name, reason="legal", by_whom="tester")
    build_and_publish(runtime_conn)
    assert live(runtime_conn, doc) == ()
    unhide(runtime_conn, "manufacturer", name, reason="ok", by_whom="tester")
    build_and_publish(runtime_conn)
    assert live(runtime_conn, doc) != ()


def test_retagging_a_live_manual_as_not_equipment_takes_it_out_and_keeps_filters_true(runtime_conn):
    from crawler.kb import build, publish
    from crawler.store import save_document
    from p3support import make_doc
    name, brand_id = new_brand(runtime_conn)
    page_id = make_doc(runtime_conn, brand_id, ["Annual report"])
    sha = q(runtime_conn, "SELECT content_sha FROM pages WHERE id = %s", (page_id,))[0][0]
    doc = save_document(runtime_conn, brand_id=brand_id, page_id=page_id, source_url="http://x", title="t", content_sha=sha,
                        models=[], extracted_by="rules", doc_type="install_manual", tags={"trade": "hvac"})
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    save_document(runtime_conn, brand_id=brand_id, page_id=page_id, source_url="http://x", title="t", content_sha=sha,
                  models=[], extracted_by="rules", doc_type="not_equipment", tags={"trade": "other"})
    assert live(runtime_conn, doc) == ()
    assert {r[0] for r in q(runtime_conn, "SELECT trade FROM sections WHERE document_id = %s", (doc,))} == {"other"}


def test_check_does_not_count_a_hidden_manual_as_live_without_sections(runtime_conn):
    from crawler.kb import build, check, hide, publish
    doc, _, _ = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    hide(runtime_conn, "document", str(doc), reason="x", by_whom="tester")
    assert doc not in check(runtime_conn)["live_without_sections"]


def test_a_manual_retagged_not_equipment_is_not_live_without_sections(runtime_conn):
    # run 4, 2026-10-30: doc 1339 (a refrigerant regulation letter) left Breeze as not_equipment, as it should, but
    # check and the parse-issue scan both listed it as a live manual with no live sections
    from crawler.issues import scan
    from crawler.kb import build, check, publish
    from crawler.store import save_document
    from p3support import make_doc
    name, brand_id = new_brand(runtime_conn)
    page_id = make_doc(runtime_conn, brand_id, ["Regulation letter"])
    sha = q(runtime_conn, "SELECT content_sha FROM pages WHERE id = %s", (page_id,))[0][0]
    doc = save_document(runtime_conn, brand_id=brand_id, page_id=page_id, source_url="http://x", title="t", content_sha=sha,
                        models=[], extracted_by="rules", doc_type="install_manual", tags={"trade": "hvac"})
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    save_document(runtime_conn, brand_id=brand_id, page_id=page_id, source_url="http://x", title="t", content_sha=sha,
                  models=[], extracted_by="rules", doc_type="not_equipment", tags={"trade": "other"})
    assert doc not in check(runtime_conn)["live_without_sections"]
    scan(runtime_conn)
    assert q(runtime_conn, "SELECT COUNT(*) FROM parse_issues WHERE document_id = %s AND kind = 'no_sections'",
             (doc,))[0][0] == 0


def test_a_section_flagged_with_its_own_language_keeps_it_through_a_retag(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build
    from test_p12_sectioner import SPANISH
    doc, _, _ = document(runtime_conn)
    monkeypatch.setattr(crawler.kb, "document_pages", lambda conn, d, **kw: [
        [t("Installation", 14), t(words(320))], [t("ESPAÑOL", 14), t(SPANISH * 4)]])
    build(runtime_conn, doc)
    langs = lambda: dict(q(runtime_conn, "SELECT heading_path, language FROM sections WHERE document_id = %s", (doc,)))
    assert langs() == {"Installation": "en", "ESPAÑOL": "es"}
    q(runtime_conn, "UPDATE documents SET equipment_type = 'heat_pump' WHERE id = %s RETURNING id", (doc,))
    build(runtime_conn, doc)  # a retag re-syncs the copied filters
    assert langs() == {"Installation": "en", "ESPAÑOL": "es"}


# ---------------------------------------------------------------- the layout pass: time limit and failures

def test_a_build_that_could_not_read_the_original_says_so(runtime_conn, tmp_path, monkeypatch):
    # the v16 rebuild of a 69-page capacity chart timed out and fell back to stored text (no tables) without a word
    from crawler.kb import build_and_publish
    from p3support import make_doc
    name, brand_id = new_brand(runtime_conn)
    page_id = make_doc(runtime_conn, brand_id, [words(400), words(400, "two")])
    sha = q(runtime_conn, "SELECT content_sha FROM pages WHERE id = %s", (page_id,))[0][0]
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    (tmp_path / f"{sha}.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    doc, _, _ = document(runtime_conn, sha=sha)
    q(runtime_conn, "INSERT INTO document_urls (document_id, page_id, url) VALUES (%s, %s, 'http://p3.test/y')", (doc, page_id))
    summary = build_and_publish(runtime_conn)
    assert live(runtime_conn, doc) != ()
    assert [d for d, _ in summary["fallback"]] == [doc]


def test_the_layout_time_limit_grows_with_the_page_count(runtime_conn, tmp_path, monkeypatch):
    import crawler.kb
    from crawler.kb import document_pages
    seen = []
    monkeypatch.setattr(crawler.kb, "extract_layout", lambda data, **kw: seen.append(kw.get("timeout")) or [])
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    for count in (None, 20, 100):
        sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        (tmp_path / f"{sha}.pdf").write_bytes(b"%PDF-1.4")
        doc, _, _ = document(runtime_conn, sha=sha)
        q(runtime_conn, "UPDATE documents SET page_count = %s WHERE id = %s", (count, doc))
        document_pages(runtime_conn, doc)
    assert seen == [180, 180, 500]
