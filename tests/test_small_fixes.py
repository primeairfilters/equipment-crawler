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
