"""P12 hand review (2026-10-27): fixes found by reading the sections of the real Ruud PDFs with the user."""
import hashlib

from p11support import crawl, serve, serve_pdf, start, world  # noqa: F401  (world is a fixture)
from p3support import make_pdf, q
from test_p12_build import document


# ---------------------------------------------------------------- text drawn as shapes

def glyph(x, y):
    """A letter drawn as a filled outline (10 points), the way some PDFs print text instead of using a font."""
    points = [(0, 0), (1, 3), (0, 6), (2, 5), (4, 6), (3, 3), (4, 0), (3, 1), (2, 0), (1, 1)]
    return f"{x} {y} m " + " ".join(f"{x + a} {y + b} l" for a, b in points[1:]) + " h f"


def blob(x, y):
    """A simple 4-point speck, like a printed background texture."""
    return f"{x} {y} m {x + 1} {y + 2} l {x + 3} {y + 1} l {x + 2} {y - 1} l h f"


def drawn_rows(count, shape=glyph):
    """count lines of 40 shapes each, one line every 10pt from the bottom of the page (below the real text)."""
    return [shape(72 + 5 * i, 100 + 10 * row) for row in range(count) for i in range(40)]


def real_lines(count):
    return [f"Line {n} of the limited warranty covers the compressor and parts" for n in range(count)]


def stored(world, path, body):
    doc = world.one("SELECT page_count, text_pages, needs_ocr FROM documents WHERE content_sha = %s",
                    (hashlib.sha256(body).hexdigest(),))
    status = world.one("SELECT extraction_status FROM pages WHERE url = %s", (world.url("ruud.test", path),))
    return doc, status


def test_a_pdf_whose_text_is_mostly_drawn_as_shapes_has_no_text_pages_and_needs_ocr(world):
    start(world)
    serve(world, "/start", "/warranty.pdf")
    body = make_pdf([real_lines(10)], [drawn_rows(10)])
    serve_pdf(world, "/warranty.pdf", body)
    crawl(world)
    assert stored(world, "/warranty.pdf", body) == (((1, 0, True),), (("skipped",),))


def test_in_a_drawn_text_pdf_only_the_pages_with_drawn_lines_lose_their_text(world):
    start(world)  # a Ruud IOM: English pages are real text, the Spanish and French pages are printed as outlines
    serve(world, "/start", "/iom.pdf")
    body = make_pdf([real_lines(10), [], real_lines(10)], [[], drawn_rows(10), []])
    serve_pdf(world, "/iom.pdf", body)
    crawl(world)
    assert stored(world, "/iom.pdf", body)[0] == ((3, 2, True),)
    assert [r[0] for r in world.one("SELECT has_text FROM page_text pt JOIN pages p ON p.id = pt.page_id WHERE p.url = %s "
                                    "ORDER BY page_number", (world.url("ruud.test", "/iom.pdf"),))] == [True, False, True]


def test_a_few_labels_drawn_on_a_diagram_beside_real_text_still_count_as_text(world):
    start(world)
    serve(world, "/start", "/spec.pdf")
    body = make_pdf([real_lines(30)], [drawn_rows(3)])
    serve_pdf(world, "/spec.pdf", body)
    crawl(world)
    assert stored(world, "/spec.pdf", body)[0] == ((1, 1, False),)


def test_a_background_texture_of_specks_is_not_drawn_text():
    from crawler.textextract import extract_pdf
    found = extract_pdf(make_pdf([real_lines(10)], [drawn_rows(15, blob)]))
    assert found.drawn_text is False


def test_a_pdf_with_drawn_text_gives_no_sections(runtime_conn, tmp_path, monkeypatch):
    from crawler.kb import build
    pdf = make_pdf([real_lines(10)], [drawn_rows(10)])
    sha = hashlib.sha256(pdf).hexdigest()
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    (tmp_path / f"{sha}.pdf").write_bytes(pdf)
    doc, _, _ = document(runtime_conn, sha=sha)
    assert build(runtime_conn, doc) is None
    assert q(runtime_conn, "SELECT COUNT(*) FROM sections WHERE document_id = %s", (doc,)) == ((0,),)


def test_preview_says_why_a_pdf_with_drawn_text_has_no_sections(runtime_conn, tmp_path):
    from test_p11_cli import cli
    path = tmp_path / "warranty.pdf"
    path.write_bytes(make_pdf([real_lines(10)], [drawn_rows(10)]))
    code, out, _ = cli(runtime_conn, "preview", str(path))
    assert code == 0
    assert "No sections" in out and "needs OCR" in out


# ---------------------------------------------------------------- boxes that are not tables

def text_at(x, y, text):
    return f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET"


def panel():
    """Boxes drawn around the page's text, one column split into rows (a brochure panel, a submittal sheet's box)."""
    return ["60 400 480 340 re S", "60 600 m 540 600 l S", "60 500 m 540 500 l S"]


def grid():
    """A real ruled table: 3 columns, 3 rows, a value in every cell."""
    ops = [f"{x} 100 m {x} 190 l S" for x in (60, 200, 340, 480)] + [f"60 {y} m 480 {y} l S" for y in (100, 130, 160, 190)]
    cells = [["Model", "MCA", "MOP"], ["RA15AZ24AJ3", "12", "20"], ["RA15AZ36AJ3", "17", "30"]]
    for r, row in enumerate(cells):
        ops += [text_at(70 + 140 * c, 170 - 30 * r, value) for c, value in enumerate(row)]
    return ops


def test_text_inside_boxes_that_are_not_a_table_is_read_once_as_text():
    from crawler.textextract import extract_layout, extract_pdf
    pdf = make_pdf([real_lines(20)], [panel()])
    items = extract_layout(pdf)[0]
    assert [i["kind"] for i in items if i["kind"] == "table"] == []
    texts = [i["text"] for i in items]
    assert all(texts.count(line) == 1 for line in real_lines(20))
    assert extract_pdf(pdf).pages[0].count("Line 7 of the limited warranty") == 1


def test_a_real_ruled_table_stays_a_table():
    from crawler.textextract import extract_layout
    tables = [i["rows"] for i in extract_layout(make_pdf([real_lines(5)], [grid()]))[0] if i["kind"] == "table"]
    assert tables == [[["Model", "MCA", "MOP"], ["RA15AZ24AJ3", "12", "20"], ["RA15AZ36AJ3", "17", "30"]]]


# ---------------------------------------------------------------- text hidden by a clipping path

def clipped(text, y, clip=(0, 700, 612, 92)):
    """Text drawn at height y inside a rectangular clip: a viewer shows it only where the clip covers it."""
    return [f"q {clip[0]} {clip[1]} {clip[2]} {clip[3]} re W n", text_at(72, y, text), "Q"]


def test_text_drawn_outside_its_clipping_path_is_not_read():
    from crawler.textextract import extract_layout, extract_pdf
    pdf = make_pdf([real_lines(5)], [clipped("Compressor crankcase hidden copy", 300)])
    assert "hidden copy" not in extract_pdf(pdf).pages[0]
    assert not any("hidden copy" in i.get("text", "") for i in extract_layout(pdf)[0])


def test_text_inside_its_clipping_path_is_read():
    from crawler.textextract import extract_pdf
    pdf = make_pdf([real_lines(5)], [clipped("Compressor crankcase shown", 760)])
    assert "Compressor crankcase shown" in extract_pdf(pdf).pages[0]


def test_a_clip_ends_with_its_saved_state():
    from crawler.textextract import extract_pdf
    pdf = make_pdf([real_lines(5)], [clipped("inside", 760) + [text_at(72, 300, "Text after the clip is restored")]])
    assert "Text after the clip is restored" in extract_pdf(pdf).pages[0]


# ---------------------------------------------------------------- rotated text (landscape pages, diagram labels)

def rotated_at(x, y, text, up=True):
    """Text turned a quarter turn: reading upwards from (x, y) (up) or downwards (not up), as Ruud prints landscape
    wiring diagrams and clearance drawings on portrait pages."""
    matrix = "0 1 -1 0" if up else "0 -1 1 0"
    return f"BT /F1 10 Tf {matrix} {x} {y} Tm ({text}) Tj ET"


def test_rotated_text_is_read_as_lines_not_one_letter_per_line():
    from crawler.textextract import extract_layout
    sentence = "IMPORTANT: When installing multiple units in an alcove ensure there is adequate ventilation"
    texts = [i["text"] for i in extract_layout(make_pdf([real_lines(3)], [[rotated_at(500, 100, sentence)]]))[0]]
    assert sentence in texts
    assert not any(len(t) <= 2 for t in texts)


def test_rotated_lines_keep_their_reading_order():
    from crawler.textextract import extract_layout
    up = [rotated_at(300, 100, "First line reading upwards"), rotated_at(320, 100, "Second line reading upwards")]
    down = [rotated_at(320, 700, "First line reading downwards", up=False),
            rotated_at(300, 700, "Second line reading downwards", up=False)]
    texts = [i["text"] for i in extract_layout(make_pdf([[], []], [up, down]))[0]]
    assert texts == ["First line reading upwards", "Second line reading upwards"]
    texts = [i["text"] for i in extract_layout(make_pdf([[], []], [up, down]))[1]]
    assert texts == ["First line reading downwards", "Second line reading downwards"]


def test_rotated_labels_far_apart_on_one_line_stay_apart():
    from crawler.textextract import extract_layout
    labels = [rotated_at(400, 100, "WALL"), rotated_at(400, 500, "Service clearance")]
    texts = [i["text"] for i in extract_layout(make_pdf([real_lines(3)], [labels]))[0]]
    assert "WALL" in texts and "Service clearance" in texts
    assert all(line in texts for line in real_lines(3))


def test_a_glyph_with_no_text_is_dropped():
    from crawler.textextract import clean
    assert clean("24(cid:31) (609.6)\n(cid:3)(cid:4)\nService") == "24 (609.6)\nService"


# ---------------------------------------------------------------- letter-spaced (tracked) display headings

def tracked_at(x, y, text, size=24, tracking=2.4):
    """A display heading set with extra space between letters (Tc), the way the Ruud brochures print theirs."""
    return f"BT /F1 {size} Tf {tracking} Tc 1 0 0 1 {x} {y} Tm ({text}) Tj 0 Tc ET"


def test_a_letter_spaced_heading_with_real_spaces_reads_as_its_words():
    from crawler.textextract import extract_layout
    texts = [i["text"] for i in extract_layout(make_pdf([real_lines(3)], [[tracked_at(72, 300, "Unit Dimensions & Weights")]]))[0]]
    assert "Unit Dimensions & Weights" in texts


def test_words_set_apart_without_space_characters_stay_apart():
    from crawler.textextract import extract_layout
    # gaps of about 0.38 em between the three runs, set by position only; the last run has real spaces
    columns = [text_at(72, 300, "Model"), text_at(103, 300, "RA15AZ24AJ3"), text_at(173, 300, "Cooling 24,000 BTU")]
    texts = [i["text"] for i in extract_layout(make_pdf([real_lines(3)], [columns]))[0]]
    assert any("Model RA15AZ24AJ3 Cooling 24,000 BTU" == t for t in texts)


def test_a_page_with_no_turned_text_skips_the_turned_text_pass(monkeypatch):
    # dedupe_chars on every page of a 69-page capacity chart pushed its layout past the time limit (399 s)
    import io
    import pdfplumber
    from crawler import pdfchild
    calls = []
    monkeypatch.setattr(pdfplumber.page.Page, "dedupe_chars", lambda self, **kw: calls.append(1) or self)
    with pdfplumber.open(io.BytesIO(make_pdf([real_lines(3)], [grid()])), laparams={}) as pdf:
        items = pdfchild.page_layout(pdf.pages[0])
    assert calls == [] and [i["kind"] for i in items].count("table") == 1
