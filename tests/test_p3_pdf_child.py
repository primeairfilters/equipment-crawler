"""P3: the PDF child process is boxed in: no secrets, bounded page text, bounded output.

Seams (all in the code as written, read before these tests were added):
- crawler.textextract.extract_pdf starts the child with subprocess.Popen(..., env=<dict>). The first
  test wraps Popen to see that dict; it still starts the real child.
- crawler.pdfchild.main() reads argv[1] and the PDF bytes from stdin and prints JSON. The second
  test runs it in-process with a stand-in pdfplumber, so no huge PDF is needed.
- crawler.textextract.PDF_MAX_OUTPUT_BYTES is read at call time; the third test lowers it.
"""
import io
import json
import sys
import types

import pytest

from p3support import locked_pdf, make_pdf

SECRETS = {"EQ_RUNTIME_PASSWORD": "runtime-pass-should-not-leak", "EQ_MIGRATE_PASSWORD": "migrate-pass-should-not-leak",
           "ANTHROPIC_API_KEY_CRAWLER": "sk-ant-should-not-leak"}


def test_the_pdf_child_gets_none_of_the_parents_secrets(monkeypatch):
    import subprocess

    from crawler import textextract

    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    seen = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen.append(kwargs.get("env"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(textextract.subprocess, "Popen", spy)
    out = textextract.extract_pdf(make_pdf([["Model RA15AZ24AJ3CA", "SEER2 15.2"]]))

    assert out.pages and "RA15AZ24AJ3CA" in out.pages[0]  # the real child ran, so what we saw was really passed to it
    assert seen and all(isinstance(env, dict) for env in seen)  # an explicit environment, not "inherit everything"
    for env in seen:  # the reader, and the OCR child on a page this short
        for key, value in SECRETS.items():
            assert key not in env
            assert value not in env.values()
        assert not [k for k in env if k.startswith(("EQ_", "ANTHROPIC", "OPENAI", "AWS_"))]


def run_child(monkeypatch, page_text, closed=None):
    from crawler import pdfchild

    class Page:
        def extract_text(self):
            return page_text

        def close(self):
            if closed is not None:
                closed.append(self)

    class Pdf:
        pages = [Page(), Page()]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.ModuleType("pdfplumber")
    fake.open = lambda handle: Pdf()
    monkeypatch.setitem(sys.modules, "pdfplumber", fake)
    quick = types.SimpleNamespace(close=lambda: None)  # pdfium's copy of a page, only asked whether it is crowded
    fast = types.ModuleType("pypdfium2")
    fast.PdfDocument = lambda data: type("Doc", (), {"__getitem__": lambda self, n: quick, "close": lambda self: None})()
    monkeypatch.setitem(sys.modules, "pypdfium2", fast)
    monkeypatch.setattr(pdfchild, "crowded", lambda page: False)
    monkeypatch.setattr(pdfchild, "_clip_aware", lambda: None)  # the stand-in has no pdfminer underneath to hook
    monkeypatch.setattr(pdfchild, "_capped_grouping", lambda: None)
    monkeypatch.setattr(pdfchild, "_fast_tables", lambda: None)
    monkeypatch.setattr(pdfchild, "_fast_objects", lambda: None)
    monkeypatch.setattr(sys, "argv", ["pdfchild", "5"])
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"%PDF-1.4 stand-in")))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    pdfchild.main()
    return json.loads(out.getvalue())["pages"]


def test_one_pages_text_is_cut_to_50000_characters(monkeypatch):
    pages = run_child(monkeypatch, "x" * 60_000)
    assert len(pages) == 2
    assert len(pages[0]) == 50_000


def test_each_page_is_closed_once_read_so_a_big_manual_fits_in_memory(monkeypatch):
    closed = []
    run_child(monkeypatch, "text", closed)  # a 37 MB service manual peaked at 1.6 GB without this, 0.2 GB with it
    assert len(closed) == 2


def test_a_short_pages_text_is_left_alone(monkeypatch):
    assert run_child(monkeypatch, "short text") == ["short text", "short text"]  # control: the cut is not applied to everything


def test_child_output_over_the_parents_limit_is_an_error(monkeypatch):
    from crawler import textextract
    monkeypatch.delenv("DOCS_DIR", raising=False)  # no kept read: the second call must read the PDF again
    pdf = make_pdf([["Model RA15AZ24AJ3CA", "SEER2 15.2", "Tonnage 2.0"]])
    assert textextract.extract_pdf(pdf).pages  # control: with the normal limit it works
    monkeypatch.setattr(textextract, "PDF_MAX_OUTPUT_BYTES", 50)
    with pytest.raises(textextract.ExtractError):
        textextract.extract_pdf(pdf)


def _kerned_pdf(words_per_line, lines=12, gap=220):
    """Words set apart by kerning (TJ offsets of `gap` thousandths of an em), with no space characters: how Ruud's
    condensing boiler manuals (docs 638, 641, 642, 655, 657) print their body text."""
    line = "[" + f" -{gap} ".join(f"({w})" for w in words_per_line) + "] TJ"
    ops = ["BT", "/F1 10 Tf", "12 TL", "72 700 Td"] + [line + " T*"] * lines + ["ET"]
    return make_pdf([[]], drawings=[ops])


WORDS = ["The", "heat", "exchanger", "has", "a", "small", "amount", "of", "combustion", "chamber", "insulation"]


def test_words_set_apart_by_kerning_are_read_as_words():
    # parse issue garbled_text (2026-10-30): pdfplumber's fixed 3-point tolerance glued words 2.2 points apart
    from crawler.textextract import extract_pdf
    text = extract_pdf(_kerned_pdf(WORDS)).pages[0]
    assert "The heat exchanger has a small amount of combustion chamber insulation" in text


def test_words_set_apart_by_a_gap_of_15_hundredths_of_the_font_are_read_as_words():
    # parse issue garbled_text 239, 240 (2026-11-08): Munters' MG50/MG90 and IceDry 30 manuals set words 1.79 points
    # apart at 11.96-point type, exactly the old SPACING_RATIO (0.15), so even the re-read glued them
    from crawler.textextract import extract_pdf
    text = extract_pdf(_kerned_pdf(WORDS, gap=150)).pages[0]
    assert "The heat exchanger has a small amount of combustion chamber insulation" in text


def test_a_pdf_that_needs_a_password_to_open_says_so():
    # 2026-11-08: 21 ClimateMaster PDFs need an open password (their own site serves them so); "could not read the
    # PDF" with a PDFium traceback read as a reader bug to fix
    from crawler.textextract import NEEDS_PASSWORD, ExtractError, extract_pdf
    with pytest.raises(ExtractError) as caught:
        extract_pdf(locked_pdf())
    assert str(caught.value) == NEEDS_PASSWORD == "the PDF needs a password to open"


def test_letters_of_one_word_are_never_split():
    from crawler.textextract import extract_pdf
    text = extract_pdf(make_pdf([["Installation instructions for the condensing boiler"] * 5])).pages[0]
    assert "Installation instructions for the condensing boiler" in text


def _wiring_pdf(pieces):
    """Page 1 a wiring diagram: labels over one drawing of `pieces` short lines (prod parse issues 53-66, 2026-11-02:
    AAON, Addison and Mitsubishi manuals with pages of 100,000 to 1,170,000 line pieces, which pdfplumber took up to
    51 s and 1.6 GB a page to read). Page 2 an ordinary page."""
    drawing = ["0.1 w"] + [f"{n % 500} {n // 500 % 700} m {n % 500 + 1} {n // 500 % 700} l" for n in range(pieces)] + ["S"]
    return make_pdf([["WIRING DIAGRAM", "Terminal R to thermostat R"], ["Refrigerant charging", "Charge by subcooling"]],
                    drawings=[drawing, []])


def test_a_page_crowded_with_drawing_is_read_quickly_with_its_text():
    from crawler.textextract import extract_pdf
    out = extract_pdf(_wiring_pdf(300_000), timeout=15)
    assert "Terminal R to thermostat R" in out.pages[0]
    assert "Charge by subcooling" in out.pages[1]


def test_a_page_crowded_with_drawing_keeps_its_lines_for_the_sections():
    from crawler.textextract import extract_layout
    pages = extract_layout(_wiring_pdf(300_000), timeout=15)
    first = [i for i in pages[0] if i["kind"] == "text"]
    assert [i["text"] for i in first] == ["WIRING DIAGRAM", "Terminal R to thermostat R"]
    assert all(i["size"] == 12.0 for i in first)
    assert [i["text"] for i in pages[1]] == ["Refrigerant charging", "Charge by subcooling"]


def _table_pdf():
    """One page holding a ruled 3-row, 2-column table (a spec sheet's data table)."""
    rules = ["0.5 w"] + [f"70 {y} m 300 {y} l" for y in (731, 716, 702, 687)] + [f"{x} 687 m {x} 731 l" for x in (70, 150, 300)] + ["S"]
    return make_pdf([["Model" + " " * 16 + "SEER2", "RA15AZ" + " " * 14 + "15.2", "RA17AZ" + " " * 14 + "17.0"]], drawings=[rules])


def test_a_pages_ruled_table_is_read_once_for_its_text_and_its_layout(monkeypatch):
    """Prod parse issues 2026-11-05: dense Daikin, Carrier and Evapco spec tables (30,000 characters a page) took
    6-8 s a page because each table was read four times, so a 40-page sheet ran past the 180 s limit."""
    import pdfplumber
    from pdfplumber.table import Table

    from crawler import pdfchild

    reads = []
    real = Table.extract
    monkeypatch.setattr(Table, "extract", lambda self, **kw: reads.append(self) or real(self, **kw))
    with pdfplumber.open(io.BytesIO(_table_pdf()), laparams={}) as pdf:
        page = pdf.pages[0]
        text = pdfchild.page_text(page)
        items = pdfchild.page_layout(page)

    assert "RA15AZ\t15.2" in text  # the table really was found and read
    assert [i["rows"] for i in items if i["kind"] == "table"] == [[["Model", "SEER2"], ["RA15AZ", "15.2"], ["RA17AZ", "17.0"]]]
    assert len(reads) == 1


def _dense_table_pdf(rows=40, cols=8):
    """A dense ruled spec table: rows x cols cells, each with its own label, except one empty cell (3, 3) and one
    merged cell (row 0, columns 1 and 2: the rule between them is not drawn)."""
    ys = [760 - r * 14 for r in range(rows + 1)]
    xs = [40 + c * 64 for c in range(cols + 1)]
    rules = ["0.5 w"] + [f"{xs[0]} {y} m {xs[-1]} {y} l" for y in ys]
    rules += [f"{x} {ys[r + 1]} m {x} {ys[r]} l" for c, x in enumerate(xs) for r in range(rows) if (r, c) != (0, 2)]
    labels = [f"BT /F1 7 Tf {xs[c] + 3} {ys[r] - 10} Td (R{r}C{c}) Tj ET"
              for r in range(rows) for c in range(cols) if (r, c) != (3, 3)]
    return make_pdf([[]], drawings=[rules + ["S"] + labels])


def test_a_dense_ruled_table_is_read_as_pdfplumber_reads_it_without_its_slow_cell_steps(monkeypatch):
    """Prod parse issues 2026-11-07: 20 Evapco spec PDFs (pages of 28,000-36,000 characters in one ruled table) ran past
    the time limit. pdfplumber found each table's cells by comparing every grid corner with every other, and read
    them by checking every character against every row: 6.3 million checks and 4.4 s for one page. The reader finds
    and reads the same cells through a sorted index, and the rows come out exactly as pdfplumber's own."""
    import pdfplumber
    import pdfplumber.table as table

    from crawler import pdfchild

    data = _dense_table_pdf()
    with pdfplumber.open(io.BytesIO(data), laparams={}) as pdf:
        expected = [t.extract() for t in pdf.pages[0].find_tables()]
    assert expected[0][0][:3] == ["R0C0", "R0C1 R0C2", None] and expected[0][3][3] == ""  # merged and empty cells

    slow = []
    real_cells, real_extract = table.intersections_to_cells, table.Table.extract
    monkeypatch.setattr(table, "intersections_to_cells", lambda *a: slow.append("cells") or real_cells(*a))
    monkeypatch.setattr(table.Table, "extract", lambda self, **kw: slow.append("extract") or real_extract(self, **kw))
    pdfchild._fast_tables()
    with pdfplumber.open(io.BytesIO(data), laparams={}) as pdf:
        page = pdf.pages[0]
        found = [rows for _, rows in pdfchild.find_tables(page)]

    assert slow == []
    assert found == expected


def test_a_pages_characters_and_drawing_come_out_as_pdfplumber_makes_them_without_resolving_plain_values(monkeypatch):
    """Prod parse issues 2026-11-08: Evapco spec PDFs of 34,000 characters a page still took 1.5 s a page to parse, half
    of it pdfplumber turning each character into its dict: a regex on the class name and a recursive resolve of every
    attribute, plain numbers and strings too. The reader makes the same dicts and resolves only what can hold a
    reference."""
    import pdfplumber
    import pdfplumber.page as plumber_page

    from crawler import pdfchild

    data = _dense_table_pdf(rows=12, cols=4)
    with pdfplumber.open(io.BytesIO(data), laparams={}) as pdf:
        expected = pdf.pages[0].objects
    assert len(expected["char"]) > 150 and expected["line"] and "rect" not in expected

    resolved = []
    real = plumber_page.resolve_all
    monkeypatch.setattr(plumber_page, "resolve_all", lambda v: resolved.append(v) or real(v))
    monkeypatch.setattr(plumber_page.Page, "process_object", plumber_page.Page.process_object)
    pdfchild._fast_objects()
    with pdfplumber.open(io.BytesIO(data), laparams={}) as pdf:
        found = pdf.pages[0].objects

    assert found == expected
    assert not [v for v in resolved if isinstance(v, (int, float, str))]


def _main_with_limit(monkeypatch, pdf, floor, per_page):
    """pdfchild.main() in-process with a time limit asked for; returns the alarms it set and what it said on stderr."""
    from crawler import pdfchild

    alarms = []
    monkeypatch.setattr(pdfchild.signal, "alarm", alarms.append)
    for hook in ("_clip_aware", "_capped_grouping", "_fast_tables", "_fast_objects"):
        monkeypatch.setattr(pdfchild, hook, lambda: None)  # main() would hook the PDF libraries for every later test
    monkeypatch.setattr(pdfchild, "_stable_ids", lambda: (lambda: None))
    monkeypatch.setattr(sys, "argv", ["pdfchild", "5", "layout", str(floor), str(per_page)])
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(pdf)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    pdfchild.main()
    return alarms, err.getvalue()


def test_the_readers_time_limit_grows_with_the_pages_it_reads(monkeypatch):
    """Prod parse issues 2026-11-07: 400-page manuals (York, Evapco) ran past one 240 s limit for any PDF, although
    nothing was wrong with them. The reader counts the pages it will read and sets its own limit: the floor, or so many
    seconds a page when that is more (only up to the page limit: a longer PDF is cut there)."""
    three = make_pdf([["Model RA15AZ"], ["SEER2 15.2"], ["Charge by subcooling"]])
    assert _main_with_limit(monkeypatch, three, 240, 100) == ([300], "pdfchild.py: time limit 300 s for 3 pages\n")
    assert _main_with_limit(monkeypatch, three, 240, 5)[0] == [240]
    eight = make_pdf([[f"Page {n}"] for n in range(8)])
    assert _main_with_limit(monkeypatch, eight, 240, 100)[0] == [500]  # cut at 5 pages


def test_a_reader_stopped_by_its_own_limit_took_too_long_and_the_crawl_waits_for_the_longest_limit(monkeypatch):
    import signal

    from crawler import textextract

    started = []

    class Child:
        pid, returncode = 4321, -signal.SIGALRM

        def __init__(self, args, **kwargs):
            started.append(args)

        def communicate(self, data=None, timeout=None):
            started.append(timeout)
            return b"", b"pdfchild.py: time limit 300 s for 60 pages\n"

    monkeypatch.setattr(textextract.subprocess, "Popen", Child)
    with pytest.raises(textextract.ExtractError) as caught:
        textextract.extract_pdf(make_pdf([["Model RA15AZ"]]))

    assert str(caught.value) == "the PDF took too long to read"
    assert caught.value.detail == "pdfchild.py: time limit 300 s for 60 pages"
    floor, per_page = textextract.PDF_TIMEOUT_SECONDS, textextract.PDF_SECONDS_PER_PAGE
    assert started[0][-4:] == [str(textextract.PDF_MAX_PAGES), "layout", str(floor), str(per_page)]
    assert started[1] == max(floor, per_page * textextract.PDF_MAX_PAGES) + textextract.CHILD_START_SECONDS


def test_a_page_the_pdf_library_cannot_read_is_read_plainly_and_the_rest_of_the_pdf_is_kept(monkeypatch):
    """Prod parse issues 101-105, 2026-11-05: pdfminer failed on one page of a Carrier data sheet and the whole PDF was
    lost. That page is read by pdfium instead (its lines, no tables)."""
    from pdfplumber.utils.exceptions import PdfminerException

    from crawler import pdfchild

    real = pdfchild.page_text
    monkeypatch.setattr(pdfchild, "page_text", lambda page: real(page) if page.page_number > 1 else (_ for _ in ()).throw(PdfminerException("bad page")))
    monkeypatch.setattr(pdfchild, "_clip_aware", lambda: None)
    monkeypatch.setattr(pdfchild, "_stable_ids", lambda: (lambda: None))
    monkeypatch.setattr(pdfchild, "_capped_grouping", lambda: None)  # main() would hook pdfminer for every later test
    monkeypatch.setattr(pdfchild, "_fast_tables", lambda: None)  # and pdfplumber's table steps
    monkeypatch.setattr(pdfchild, "_fast_objects", lambda: None)  # and its object dicts
    monkeypatch.setattr(sys, "argv", ["pdfchild", "5", "layout"])
    pdf = make_pdf([["Model RA15AZ24AJ3CA", "SEER2 15.2"], ["Refrigerant charging", "Charge by subcooling"]])
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(pdf)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    pdfchild.main()
    answer = json.loads(out.getvalue())

    assert answer["pages"] == ["Model RA15AZ24AJ3CA\nSEER2 15.2", "Refrigerant charging\nCharge by subcooling"]
    assert [i["text"] for i in answer["layout"][0]] == ["Model RA15AZ24AJ3CA", "SEER2 15.2"]


def _labels_pdf(rows, cols=10):
    """One page of small labels scattered in a grid, each its own text box (a Daikin spec sheet's diagram page)."""
    labels = [f"BT /F1 6 Tf {40 + c * 55} {760 - r * 12} Td (L{r:02d}x{c}) Tj ET" for r in range(rows) for c in range(cols)]
    return make_pdf([[]], drawings=[labels])


def _grouping_spy(monkeypatch):
    """pdfminer's box grouping, which refuses more than 500 boxes the way it ran on prod: 2.3 million comparisons and
    11-18 s for one page of 1,400-1,700 labels (parse issues 2026-11-05)."""
    import pdfminer.layout as layout

    given = []
    real = layout.LTLayoutContainer.group_textboxes

    def spy(self, laparams, boxes):
        given.append(len(boxes))
        assert len(boxes) <= 500, "grouped every box against every other"
        return real(self, laparams, boxes)

    monkeypatch.setattr(layout.LTLayoutContainer, "group_textboxes", spy)
    return given


def test_a_page_of_too_many_text_boxes_is_read_top_to_bottom_without_grouping_them(monkeypatch):
    import pdfplumber

    from crawler import pdfchild

    given = _grouping_spy(monkeypatch)
    pdfchild._capped_grouping()
    with pdfplumber.open(io.BytesIO(_labels_pdf(60)), laparams={}) as pdf:
        lines = [text for _, text, _ in pdfchild._box_lines(pdf.pages[0], [])]

    assert given == []  # 600 boxes never reached the grouping
    assert len(lines) == 600
    assert lines[0] == "L00x0" and lines.index("L00x9") < lines.index("L59x0")  # the top row first


def test_a_page_of_few_text_boxes_is_still_grouped_into_reading_order(monkeypatch):
    import pdfplumber

    from crawler import pdfchild

    given = _grouping_spy(monkeypatch)
    pdfchild._capped_grouping()
    with pdfplumber.open(io.BytesIO(_labels_pdf(5)), laparams={}) as pdf:
        lines = [text for _, text, _ in pdfchild._box_lines(pdf.pages[0], [])]

    assert given == [50]
    assert len(lines) == 50
