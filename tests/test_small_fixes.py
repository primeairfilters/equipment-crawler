"""Small fixes (user, 2026-10-28): a host approved during a run reaches that run; `stop` stops a run already going and
`resume` undoes it; `status` counts every document (saying how many are not tagged yet) and says how many pages no
text could be read from, instead of "needing OCR" (OCR has read every page it can)."""
import hashlib
import uuid

import pytest

from p11support import crawl, serve, serve_pdf, start, state, world  # noqa: F401  (world is a fixture)
from p3support import new_brand, q
from test_p11_cli import cli


# ---------------------------------------------------------------- a host approved mid-run

def test_a_host_approved_during_a_run_is_crawled_in_that_run(world):
    start(world)
    serve(world, "/start", (world.url("files.other.test", "/manual.pdf"), "Manual"), "/a")
    serve(world, "/a", "/b")
    serve(world, "/b")
    serve_pdf(world, "/manual.pdf", host="files.other.test")
    pages = []

    def approve_after_first_page():
        pages.append(1)
        if len(pages) == 1:
            world.approve("files.other.test")

    crawl(world, after_page=approve_after_first_page)
    assert world.site.hits(host="files.other.test") != []
    assert state(world, "/manual.pdf", host="files.other.test")[0] == "fetched"


def test_without_a_new_approval_an_off_site_pdf_is_still_never_requested(world):
    start(world)
    serve(world, "/start", (world.url("files.other.test", "/manual.pdf"), "Manual"), "/a")
    serve(world, "/a")
    crawl(world, after_page=lambda: None)
    assert world.site.hits(host="files.other.test") == []
    assert state(world, "/manual.pdf", host="files.other.test")[0] == "out_of_scope"


# ---------------------------------------------------------------- stop and resume

def test_stopping_a_brand_stops_a_run_already_going(world):
    from crawler.brands import stop
    start(world)
    serve(world, "/start", "/a", "/b", "/c")
    for path in ("/a", "/b", "/c"):
        serve(world, path)
    crawl(world, after_page=lambda: stop(world.conn, world.brand))
    assert len(world.site.hits(host="ruud.test")) <= 2  # the start page (and its robots.txt), nothing after the stop


def test_resume_puts_a_stopped_brand_and_its_start_addresses_back(world):
    from crawler.brands import stop
    start(world)
    stop(world.conn, world.brand)
    code, out, _ = cli(world.conn, "resume", world.brand)
    assert code == 0 and f"Resumed {world.brand}." in out
    assert world.one("SELECT status FROM brands WHERE id = %s", (world.brand_id,)) == (("active",),)
    assert world.one("SELECT DISTINCT status FROM start_urls WHERE brand_id = %s", (world.brand_id,)) == (("active",),)
    serve(world, "/start")
    crawl(world)
    assert world.site.hits(host="ruud.test") != []


def test_resume_puts_a_stopped_host_back(world):
    from crawler.brands import resume, stop
    stop(world.conn, "ruud.test")
    assert resume(world.conn, "ruud.test") == "host"
    assert world.one("SELECT status FROM brand_domains WHERE brand_id = %s AND host = 'ruud.test'",
                     (world.brand_id,)) == (("approved",),)


def test_resume_of_something_unknown_says_so(runtime_conn):
    code, _, err = cli(runtime_conn, "resume", "No Such Brand " + uuid.uuid4().hex[:6])
    assert code != 0 and "No brand or host called" in err


# ---------------------------------------------------------------- status

def documents_for(conn, *, tagged, untagged, ocr_pages=None):
    """A brand with that many tagged and untagged documents; ocr_pages: (pages, text pages) for one more, tagged."""
    name, brand_id = new_brand(conn)
    host_id = q(conn, "INSERT INTO hosts (host) VALUES (%s) ON CONFLICT (host) DO UPDATE SET host = EXCLUDED.host "
                      "RETURNING id", (f"{uuid.uuid4().hex[:8]}.test",))[0][0]
    specs = [("install_manual", 4, 4)] * tagged + [(None, 4, 4)] * untagged
    if ocr_pages:
        specs.append(("install_manual", *ocr_pages))
    for doc_type, pages, text_pages in specs:
        url = f"http://s.test/{uuid.uuid4().hex}.pdf"
        page_id = q(conn, "INSERT INTO pages (brand_id, host_id, url, url_sha) VALUES (%s, %s, %s, %s) RETURNING id",
                    (brand_id, host_id, url, hashlib.sha256(url.encode()).hexdigest()))[0][0]
        doc = q(conn, "INSERT INTO documents (content_sha, doc_type, page_count, text_pages, needs_ocr, state) "
                      "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (hashlib.sha256(uuid.uuid4().bytes).hexdigest(), doc_type, pages, text_pages, text_pages < pages,
                 "tagged" if doc_type else "new"))[0][0]
        q(conn, "INSERT INTO document_urls (document_id, page_id, url) VALUES (%s, %s, %s)", (doc, page_id, url))
    return name


def status_of(conn, name):
    code, out, _ = cli(conn, "status")
    line = [ln for ln in out.splitlines() if name in ln][0]
    detail = out.split(line, 1)[1].split("\n\n")[0]
    return line, detail


def test_status_counts_every_document_and_says_how_many_are_not_tagged_yet(runtime_conn):
    from crawler.brands import status
    name = documents_for(runtime_conn, tagged=2, untagged=1)
    row = next(r for r in status(runtime_conn) if r["brand"] == name)
    assert row["documents"] == 3 and row["untagged"] == 1
    line, _ = status_of(runtime_conn, name)
    assert "3 documents (1 not tagged yet)" in line


def test_status_with_every_document_tagged_says_only_the_count(runtime_conn):
    name = documents_for(runtime_conn, tagged=2, untagged=0)
    line, _ = status_of(runtime_conn, name)
    assert line.endswith(", 2 documents") or ", 2 documents," in line
    assert "not tagged" not in line


def test_status_says_how_many_pages_no_text_was_read_from_not_needing_ocr(runtime_conn):
    from crawler.brands import status
    name = documents_for(runtime_conn, tagged=1, untagged=0, ocr_pages=(10, 7))
    row = next(r for r in status(runtime_conn) if r["brand"] == name)
    assert row["needs_ocr"] == 1 and row["pages_without_text"] == 3
    _, detail = status_of(runtime_conn, name)
    assert "1 manual with 3 pages no text was read from" in detail and "needing OCR" not in detail


def test_status_spells_the_plural_of_address():
    from crawler.__main__ import _missed_lines
    assert _missed_lines({"starts_at_limit": 2}) == ["2 start addresses hit the page limit"]
    assert _missed_lines({"starts_at_limit": 1}) == ["1 start address hit the page limit"]


# ---------------------------------------------------------------- headings from the text layer (mental notes)

def text_line(text, size=10.0):
    return {"kind": "text", "text": text, "size": size, "bold": False}


FILLER_TEXT = ("Connect the gauge manifold to the service ports and run the system for fifteen minutes before reading "
               "the pressures. ") * 18


def text_headings(*titles):
    from crawler.sections import build_sections
    page = []
    for title in titles:
        page += [text_line(title, size=14.0), text_line(FILLER_TEXT)]
    cover = [text_line("Read every instruction in this manual before you install or service the unit. " * 40)]
    return {s.heading_path for s in build_sections([cover, page])}



@pytest.mark.parametrize("junk", ["! WARNING", "! DANGER!", "! CAUTION", "! NOTICE", "⚠ WARNING",
                                  "! DANGER: Hotter water increases the", "]AGL-AGN-AHM MODELS)*([ 9.0",
                                  "> Return to Table of Contents EcoNet® Smart Thermostat User Guide 10"])
def test_an_icon_caption_or_a_mirrored_or_navigation_line_is_no_heading(junk):
    paths = text_headings("Refrigerant Charging", junk)
    assert not any(junk in p for p in paths) and any("Refrigerant Charging" in p for p in paths)


@pytest.mark.parametrize("title", ["Caution Statements", "(1) Pipe cutting", "4.1 Pipe Material", "Wiring Diagram"])
def test_ordinary_headings_stay(title):
    assert any(title in p for p in text_headings("Refrigerant Charging", title))


def test_a_letter_spaced_caption_is_no_heading():
    paths = text_headings("Refrigerant Charging", "WA R N I N G")
    assert not any("WA R N I N G" in p for p in paths)


def test_a_footer_on_one_language_half_of_a_manual_is_dropped():
    """An English/French guide: the English footer is on 6 of 14 pages, under half, but still a footer."""
    from crawler.sections import build_sections
    pages = []
    for n in range(14):
        english = n < 7
        body = (f"Page {n} explains the thermostat settings and the steps to follow when connecting it to WiFi. " * 20
                if english else f"La page {n} explique les réglages du thermostat et les étapes de connexion WiFi. " * 20)
        page = [text_line("Connecting to WiFi" if english else "Connexion WiFi", size=13.0), text_line(body)]
        if english and n > 0:
            page += [text_line("> Return to Table of Contents", size=12.0),
                     text_line(f"EcoNet® Smart Thermostat User Guide {n}", size=12.0)]
        pages.append(page)
    sections = build_sections(pages)
    assert not any("User Guide" in s.heading_path or "Return to Table" in s.heading_path for s in sections)
    assert not any("EcoNet® Smart Thermostat User Guide" in s.body for s in sections)


def test_a_fixed_label_at_the_bottom_of_each_page_stays_a_heading():
    """A spec book's series label sits at the foot of each page and is the only thing saying which model the tables
    are for; it has no changing page number, so it is not a footer (seen on Ruud document 75 with a rule that dropped
    every repeated bottom line)."""
    from crawler.sections import build_sections
    words = ["cooling", "heating", "airflow", "capacity", "pressure", "voltage", "charge", "defrost", "sensor",
             "compressor"]
    pages = []
    for n in range(20):  # the first label is on 8 of 20 pages: under half, over FURNITURE_PAGES
        series = ("Single zone indoor units series" if n < 8 else "Dual zone indoor units series" if n < 16
                  else "Triple zone indoor units series")  # differ in words: edge lines are compared without digits
        body = [text_line(f"{words[(n + k) % 10]} {words[(n * 3 + k) % 10]} data for this unit. " * 30) for k in range(4)]
        pages.append(body + [text_line(series, size=14.0)])
    assert any("Single zone indoor units series" in s.heading_path or "Single zone indoor units series" in s.body
               for s in build_sections(pages))


def test_a_heading_line_starting_with_an_ampersand_finishes_the_heading_above():
    from crawler.sections import build_sections
    cover = [text_line("Read every instruction in this manual before you install or service the unit. " * 40)]
    page = [text_line("PRECISE PERFORMANCE", size=30.0), text_line("& STATE-OF-THE-ART FEATURES", size=22.0),
            text_line(FILLER_TEXT)]
    paths = {s.heading_path for s in build_sections([cover, page])}
    assert "PRECISE PERFORMANCE & STATE-OF-THE-ART FEATURES" in paths


def test_a_heading_hyphenated_over_lines_is_joined_into_whole_words():
    from crawler.sections import build_sections
    cover = [text_line("Read every instruction in this manual before you install or service the unit. " * 40)]
    page = [text_line("MULTIPLE HEAT PUMP CONFIGURA-", size=16.0), text_line("TION", size=16.0),
            text_line("Set the configuration on each heat pump. " + FILLER_TEXT)]
    paths = {s.heading_path for s in build_sections([cover, page])}
    assert "MULTIPLE HEAT PUMP CONFIGURATION" in paths


def test_a_dash_at_a_heading_lines_end_is_kept_when_the_joined_word_is_not_a_word_of_the_document():
    from crawler.sections import build_sections
    cover = [text_line("Read every instruction in this manual before you install or service the unit. " * 40)]
    page = [text_line("COMMISSIONING PROCEDURE-", size=16.0), text_line("BMS CONFIGURATION", size=16.0),
            text_line(FILLER_TEXT)]
    paths = {s.heading_path for s in build_sections([cover, page])}
    assert "COMMISSIONING PROCEDURE- BMS CONFIGURATION" in paths


@pytest.mark.parametrize("junk", ["*YES", "*S or V", "* Crankcase heater"])
def test_a_footnote_line_is_no_heading(junk):
    paths = text_headings("Refrigerant Charging", junk)
    assert not any(junk in p for p in paths)


def test_a_navigation_line_repeated_at_the_foot_of_pages_is_dropped():
    from crawler.sections import build_sections
    pages = []
    for n in range(20):  # on 7 of 20 pages: under half, like one language half of the EcoNet guide
        body = [text_line(f"Step {n}: open the settings menu and choose option {n} for the thermostat schedule. " * 25)]
        pages.append([text_line("Schedules", size=13.0)] + body
                     + [text_line("> Return to Table of Contents", size=12.0) if n < 7 else text_line("Notes page", 12.0),
                        text_line(f"EcoNet® Smart Thermostat User Guide {n}", size=12.0)])
    assert not any("Return to Table of Contents" in s.body for s in build_sections(pages))


# ---------------------------------------------------------------- repeatable reading order (mental notes)

def test_the_pdf_reader_numbers_text_boxes_in_order_instead_of_by_memory_address():
    """pdfminer breaks ties between equally distant text boxes with id() (a memory address), so lines at one height
    could come out in a different order between reads. The reader gives pdfminer's layout module a stable id."""
    import importlib.util
    from pathlib import Path
    import pdfminer.layout
    spec = importlib.util.spec_from_file_location("pdfchild_under_test",
                                                  Path(__file__).parent.parent / "crawler" / "pdfchild.py")
    child = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(child)
    try:
        child._stable_ids()
        first, second = object(), object()
        assert pdfminer.layout.id(first) < pdfminer.layout.id(second)
        assert pdfminer.layout.id(first) == pdfminer.layout.id(first)
    finally:
        vars(pdfminer.layout).pop("id", None)


def test_reading_the_same_pdf_twice_gives_the_same_layout():
    from crawler.textextract import _run_child
    from p3support import make_pdf
    pdf = make_pdf([["Model RA15AZ  Cooling 24000  Heating 22000", "Voltage 208/230  MCA 15  MOP 25"]] * 3)
    assert _run_child(pdf, ["200", "layout"], 60, 1024 ** 3)["layout"] == \
        _run_child(pdf, ["200", "layout"], 60, 1024 ** 3)["layout"]


def test_the_stable_ids_are_let_go_after_each_page():
    """Holding every numbered object until the reader exits kept a whole manual's layout in memory (a 41 MB manual
    peaked at 719 MB instead of 548 MB); the numbering is only needed within a page."""
    import importlib.util
    import gc
    import weakref
    from pathlib import Path
    import pdfminer.layout
    spec = importlib.util.spec_from_file_location("pdfchild_under_test",
                                                  Path(__file__).parent.parent / "crawler" / "pdfchild.py")
    child = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(child)

    class Box:
        pass

    try:
        next_page = child._stable_ids()
        box = Box()
        pdfminer.layout.id(box)
        gone = weakref.ref(box)
        del box
        next_page()
        gc.collect()
        assert gone() is None
        assert pdfminer.layout.id(object()) == 0  # numbering starts again on each page
    finally:
        vars(pdfminer.layout).pop("id", None)


# ---------------------------------------------------------------- PDFs served as octet-stream (Ruud batch 3)

from test_p2_fetch import get, ruud  # noqa: E402,F401  (ruud is a fixture)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@pytest.mark.parametrize("kind", ["application/octet-stream", "binary/octet-stream"])
def test_a_pdf_address_served_as_octet_stream_is_kept_as_a_pdf(ruud, kind):
    """files.myrheem.com serves some manuals as application/octet-stream (6 Ruud PDFs failed on it, 2026-10-28)."""
    ruud.site.route("/manual.pdf", PDF_BYTES, headers={"Content-Type": kind})
    result = get(ruud, "/manual.pdf")
    assert result.outcome == "fetched" and result.body == PDF_BYTES and result.is_pdf


def test_an_octet_stream_pdf_address_without_the_pdf_signature_is_rejected(ruud):
    ruud.site.route("/manual.pdf", b"PK\x03\x04 a zip", headers={"Content-Type": "application/octet-stream"})
    assert (get(ruud, "/manual.pdf").reason) == "not_pdf"


def test_octet_stream_at_an_address_that_is_not_a_pdf_is_still_refused(ruud):
    ruud.site.route("/setup.exe", b"MZ\x90\x00", headers={"Content-Type": "application/octet-stream"})
    result = get(ruud, "/setup.exe")
    assert (result.outcome, result.reason) == ("failed", "bad_content_type") and not result.body


def test_a_60_mb_pdf_is_fetched(ruud):
    """Floating Air R-454B service manuals are 52-65 MB; the limit is 100 MB (user, 2026-10-28)."""
    big = b"%PDF-1.7\n" + b"0" * (60 * 1024 * 1024)
    ruud.site.route("/service-manual.pdf", big, headers={"Content-Type": "application/pdf"})
    result = get(ruud, "/service-manual.pdf")
    assert result.outcome == "fetched" and len(result.body) == len(big)


# ---------------------------------------------------------------- an untagged twin takes the tags (user decision)

def twin_documents(conn, form_line="Form No. A22-336 Rev. 4", old_on=None):
    """Two different PDF files with the same text: two documents. Tagging reads the text once. old_on: the twin
    (0 or 1) whose address served an earlier PDF first (a page keeps a document_urls row per version); returns
    its document id last."""
    from crawler.documents import record_pdf
    from p3support import make_page, make_pdf
    name, brand_id = new_brand(conn)
    lines = ["Ruud RA15AZ Installation Instructions", "Model RA15AZ24AJ3CA", form_line, "SEER2 15.2"]
    docs, old = [], None
    for n, drawings in enumerate((None, [["0 0 m 10 10 l S"]])):  # a drawn line: other bytes, same text
        body = make_pdf([lines], drawings=drawings)
        url = f"http://p3.test/twin{n}.pdf"
        page_id = make_page(conn, brand_id, body, is_pdf=True, url=url)
        if n == old_on:
            old = record_pdf(conn, brand_id, page_id, url, make_pdf([["Ruud RA15AZ Installation Instructions Rev. A"]]))
        docs.append(record_pdf(conn, brand_id, page_id, url, body))
    return (name, docs) if old_on is None else (name, docs, old)


def tags_answer():
    return {"doc_type": "install_manual", "models": ["RA15AZ24AJ3CA"], "trade": "hvac",
            "equipment_type": "air_conditioner", "language": "en", "form_number": "A22-336",
            "title": "RA15AZ Installation Instructions"}


def test_a_document_with_the_same_text_as_a_tagged_one_takes_its_tags(runtime_conn):
    from fakeai import FakeAI
    from p3support import settle
    name, (one, two) = twin_documents(runtime_conn)
    assert one != two
    fake = FakeAI(lambda r: tags_answer())
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1  # the text was read once
    rows = q(runtime_conn, "SELECT doc_type, trade, equipment_type, language, title, state FROM documents "
                           "WHERE id IN (%s, %s) ORDER BY id", (one, two))
    assert rows[0] == rows[1] and rows[0][0] == "install_manual" and rows[1][5] == "tagged"
    assert q(runtime_conn, "SELECT COUNT(*) FROM document_models WHERE document_id = %s", (two,)) == ((1,),)


def test_twins_with_the_same_form_number_keep_one_live(runtime_conn):
    """The form-number rule (D55) links the second twin to the first, so search does not show the same text twice."""
    from fakeai import FakeAI
    from p3support import settle
    name, (one, two) = twin_documents(runtime_conn)
    settle(runtime_conn, FakeAI(lambda r: tags_answer()), name)
    replaces = dict(q(runtime_conn, "SELECT id, replaces_id FROM documents WHERE id IN (%s, %s)", (one, two)))
    assert sorted(v for v in replaces.values() if v) in ([one], [two])


def test_an_older_version_on_the_copying_page_is_not_tagged(runtime_conn):
    from fakeai import FakeAI
    from p3support import settle
    name, (one, two), old = twin_documents(runtime_conn, old_on=1)
    settle(runtime_conn, FakeAI(lambda r: tags_answer()), name)
    assert q(runtime_conn, "SELECT doc_type, state FROM documents WHERE id = %s", (old,)) == ((None, "new"),)
    assert q(runtime_conn, "SELECT doc_type FROM documents WHERE id = %s", (two,)) == (("install_manual",),)


def test_tags_are_copied_from_the_twin_page_s_current_version_not_an_older_one(runtime_conn):
    from fakeai import FakeAI
    from p3support import settle
    name, (one, two), old = twin_documents(runtime_conn, old_on=0)
    q(runtime_conn, "UPDATE documents SET doc_type = 'spec_sheet', tagged_by = 'ai:claude-haiku-5-5', state = 'tagged' WHERE id = %s RETURNING id", (old,))
    settle(runtime_conn, FakeAI(lambda r: tags_answer()), name)
    assert q(runtime_conn, "SELECT doc_type FROM documents WHERE id = %s", (two,)) == (("install_manual",),)


def test_tags_are_not_copied_from_a_retired_twin(runtime_conn):
    """A retired twin's form number would make the copy replace the newer version that retired it."""
    from crawler.extract import copy_twin_tags
    from fakeai import FakeAI
    from p3support import settle
    name, (one, two) = twin_documents(runtime_conn)
    settle(runtime_conn, FakeAI(lambda r: tags_answer()), name)
    q(runtime_conn, "UPDATE documents SET doc_type = NULL, state = 'new', form_number = NULL, replaces_id = NULL "
                    "WHERE id = %s RETURNING id", (two,))
    q(runtime_conn, "UPDATE documents SET state = 'retired' WHERE id = %s RETURNING id", (one,))
    assert copy_twin_tags(runtime_conn) == 0
    assert q(runtime_conn, "SELECT doc_type FROM documents WHERE id = %s", (two,)) == ((None,),)


def test_a_twin_of_a_hidden_manual_takes_its_tags_and_is_hidden_too(runtime_conn):
    """Like a new version of a hidden manual (D56): the same text under another file stays hidden (user, 2026-10-28)."""
    from crawler.extract import copy_twin_tags
    from crawler.kb import hide
    from fakeai import FakeAI
    from p3support import settle
    name, (one, two) = twin_documents(runtime_conn)
    settle(runtime_conn, FakeAI(lambda r: tags_answer()), name)
    q(runtime_conn, "UPDATE documents SET doc_type = NULL, state = 'new' WHERE id = %s RETURNING id", (two,))
    hide(runtime_conn, "document", one, reason="wrong brand", by_whom="user")
    assert copy_twin_tags(runtime_conn) == 1
    assert q(runtime_conn, "SELECT doc_type, hidden_reason FROM documents WHERE id = %s", (two,)) == (
        ("install_manual", "wrong brand"),)
    assert q(runtime_conn, "SELECT hidden, reason FROM visibility_log WHERE target_type = 'document' AND target_id = %s",
             (two,)) == ((True, "wrong brand"),)


def test_short_top_level_sections_merged_together_keep_the_first_heading():
    """Brochure pages: two short top-level topics merge (each under MIN_WORDS) and used to get an empty path."""
    from crawler.sections import build_sections
    cover = [text_line("Read every instruction in this manual before you install or service the unit. " * 40)]
    short = "Floating Air mini-split systems deliver precise comfort and quiet operation. " * 8
    page = [text_line("PRECISE PERFORMANCE", size=30.0), text_line(short),
            text_line("EASY-TO-CLEAN DESIGN", size=30.0), text_line(short.replace("comfort", "service"))]
    paths = [s.heading_path for s in build_sections([cover, page]) if s.page_from == 2]
    assert paths == ["PRECISE PERFORMANCE"]


# ---------------------------------------------------------------- why a fetch failed; longer waits (2026-10-28)

def page_failure(world, path):
    return world.one("SELECT fetch_state, fail_reason, fail_count, next_check_at FROM pages WHERE url = %s",
                     (world.url("ruud.test", path),))[0]


def test_a_failed_fetch_records_why_and_a_later_success_clears_it(ruud):
    """Three empty Ruud PDFs failed in batch 4 and only outside requests showed why (the reason was not stored)."""
    ruud.site.route("/manual.pdf", b"", headers={"Content-Type": "application/pdf"})
    get(ruud, "/manual.pdf")
    assert page_failure(ruud, "/manual.pdf")[:3] == ("failed", "not_pdf", 1)
    ruud.site.route("/manual.pdf", PDF_BYTES, headers={"Content-Type": "application/pdf"})
    get(ruud, "/manual.pdf")
    assert page_failure(ruud, "/manual.pdf")[:3] == ("fetched", None, 0)


def test_each_failure_in_a_row_doubles_the_wait_up_to_30_days(ruud):
    from datetime import timedelta
    waits = []
    for _ in range(7):
        get(ruud, "/gone")
        waits.append(page_failure(ruud, "/gone")[3] - ruud.clock.now())
    assert waits == [timedelta(days=d) for d in (1, 2, 4, 8, 16, 30, 30)]


def test_status_says_why_pages_failed():
    from crawler.__main__ import _missed_lines
    lines = _missed_lines({"failed_reasons": {"http_404": 15, "not_pdf": 3, None: 1}})
    assert lines == ["failed: http_404 15, not_pdf 3, reason not recorded 1"]


def test_status_counts_failed_pages_by_reason(ruud):
    from crawler.brands import status
    ruud.site.route("/manual.pdf", b"", headers={"Content-Type": "application/pdf"})
    get(ruud, "/manual.pdf")
    get(ruud, "/gone")
    name = ruud.one("SELECT name FROM brands WHERE id = %s", (ruud.brand_id,))[0][0]
    row = next(r for r in status(ruud.conn) if r["brand"] == name)
    assert row["failed_reasons"] == {"http_404": 1, "not_pdf": 1}


def tag_with_form(conn, doc, form="G22-546-SFS"):
    from crawler.store import save_document
    sha, page_id, url, brand_id = q(conn, "SELECT d.content_sha, du.page_id, du.url, p.brand_id FROM documents d "
                                          "JOIN document_urls du ON du.document_id = d.id JOIN pages p ON p.id = du.page_id "
                                          "WHERE d.id = %s LIMIT 1", (doc,))[0]
    save_document(conn, brand_id=brand_id, page_id=page_id, source_url=url, title="RA15AZ", content_sha=sha, models=[],
                  extracted_by="ai:claude-haiku-5-5", doc_type="spec_sheet",
                  tags={"language": "en", "form_number": form})


def replaces(conn, *docs):
    return [q(conn, "SELECT replaces_id FROM documents WHERE id = %s", (d,))[0][0] for d in docs]


def test_the_form_number_rule_links_twins_tagged_newest_first(runtime_conn):
    """890 and 896 (form G22-546-SFS) were tagged in one pass, the newer first: when 896 looked for the version it
    replaces, 890 had no form number yet, and 890 only looks for older documents. Both went live (2026-10-28)."""
    name, (one, two) = twin_documents(runtime_conn)
    tag_with_form(runtime_conn, two)
    tag_with_form(runtime_conn, one)
    assert replaces(runtime_conn, one, two) == [None, one]


def test_the_form_number_rule_makes_a_chain_when_the_newest_is_tagged_before_the_middle_one(runtime_conn):
    """59, then 1074 and 1079 (form M22-2527) tagged in one pass, 1079 first: both pointed at 59 and both went live
    (2026-10-29). The chain is 59 <- 1074 <- 1079."""
    name, (middle, newest), oldest = twin_documents(runtime_conn, old_on=0)
    tag_with_form(runtime_conn, oldest)
    tag_with_form(runtime_conn, newest)
    tag_with_form(runtime_conn, middle)
    assert replaces(runtime_conn, oldest, middle, newest) == [None, oldest, middle]
