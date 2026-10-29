"""P3: text out of HTML and PDF, stored in page_text, and the free filter that decides what goes to AI."""
import hashlib

import pytest

from p3support import make_pdf, make_page, new_brand, q, spec_html

MODEL = "RA15AZ24AJ3CA"


# ---------------------------------------------------------------- HTML

def test_html_text_drops_scripts_and_styles_and_makes_table_rows_tab_separated():
    from crawler.textextract import extract_html
    body = (b"<html><head><title>RA15AZ spec</title><style>.x{color:red}</style>"
            b"<script>alert('hidden')</script></head><body><h1>Air  conditioner</h1>"
            b"<table><tr><th>Model</th><th>RA15AZ24AJ3</th><th>RA15AZ30AJ3</th></tr>"
            b"<tr><td>SEER2</td><td>15.2</td><td>15.0</td></tr></table>"
            b"<p>Fish &amp; chips&nbsp;here</p></body></html>")
    out = extract_html(body)
    text = "\n".join(out.pages)
    assert out.title == "RA15AZ spec"
    assert "Model\tRA15AZ24AJ3\tRA15AZ30AJ3" in text
    assert "SEER2\t15.2\t15.0" in text
    assert "alert" not in text and "color:red" not in text
    assert "Fish & chips here" in text
    assert "Air conditioner" in text  # runs of spaces collapsed


def test_text_normalises_ligatures():
    from crawler.textextract import extract_html
    out = extract_html("<html><body><p>Eﬃciency and ﬁnal</p></body></html>".encode())
    assert "Efficiency and final" in "\n".join(out.pages)


def test_the_same_content_gives_the_same_text_even_if_the_markup_differs():
    from crawler.textextract import extract_html
    a = extract_html(b"<html><body><p>Model RA15AZ24AJ3CA</p><p>SEER2 15.2</p></body></html>")
    b = extract_html(b"<html>\n  <body class='x'>\n <p>Model   RA15AZ24AJ3CA</p>\n\n<p> SEER2 15.2 </p>\n</body>\n</html>")
    assert a.pages == b.pages


# ---------------------------------------------------------------- PDF (parsed in a child process)

def test_pdf_text_comes_back_one_entry_per_page():
    from crawler.textextract import extract_pdf
    pdf = make_pdf([[f"Model {MODEL}", "SEER2 15.2"], ["General Data", "Tonnage 2.0"]])
    out = extract_pdf(pdf)
    assert len(out.pages) == 2
    assert MODEL in out.pages[0] and "SEER2 15.2" in out.pages[0]
    assert "Tonnage 2.0" in out.pages[1]


def test_pdf_text_is_the_same_on_two_extractions():
    from crawler.textextract import extract_pdf
    pdf = make_pdf([[f"Model {MODEL}", "SEER2 15.2"]])
    assert extract_pdf(pdf).pages == extract_pdf(pdf).pages


def test_a_pdf_over_the_page_cap_is_cut_or_refused_never_read_whole():
    from crawler.textextract import ExtractError, extract_pdf
    pdf = make_pdf([["one"], ["two"], ["three"]])
    try:
        out = extract_pdf(pdf, max_pages=1)
    except ExtractError:
        return
    assert len(out.pages) <= 1


def test_something_that_is_not_a_pdf_is_an_extract_error():
    from crawler.textextract import ExtractError, extract_pdf
    with pytest.raises(ExtractError):
        extract_pdf(b"%PDF-1.4\nthis is not really a pdf")


def test_a_pdf_that_takes_too_long_is_stopped():
    from crawler.textextract import ExtractError, extract_pdf
    pdf = make_pdf([[f"Model {MODEL}"]])
    assert extract_pdf(pdf).pages  # control: with the normal time limit it works
    with pytest.raises(ExtractError):
        extract_pdf(pdf, timeout=0.001)


# ---------------------------------------------------------------- index_page: page_text rows and status

def test_index_page_stores_an_html_pages_text(runtime_conn):
    """HTML text is kept for the free gap-fill check, and the page ends 'skipped': HTML is never AI-read."""
    name, brand_id = new_brand(runtime_conn)
    page_id = make_page(runtime_conn, brand_id, spec_html(MODEL))
    rows = q(runtime_conn, "SELECT page_number, title, text_sha, body FROM page_text WHERE page_id = %s", (page_id,))
    assert len(rows) == 1
    number, title, sha, body = rows[0]
    assert number == 1 and title == f"{MODEL} spec sheet"
    assert MODEL in body and "SEER2\t15.2" in body
    assert sha == hashlib.sha256(body.encode()).hexdigest()
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("skipped",),)


def test_index_page_stores_a_pdfs_text_and_queues_it(runtime_conn):
    from p3support import spec_pdf
    name, brand_id = new_brand(runtime_conn)
    page_id = make_page(runtime_conn, brand_id, spec_pdf(MODEL), is_pdf=True)
    rows = q(runtime_conn, "SELECT page_number, text_sha, body FROM page_text WHERE page_id = %s", (page_id,))
    assert len(rows) == 1
    number, sha, body = rows[0]
    assert number == 1 and MODEL in body and "SEER2 15.2" in body
    assert sha == hashlib.sha256(body.encode()).hexdigest()
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("waiting",),)


def test_index_page_skips_a_page_that_fails_the_free_filter(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = make_page(runtime_conn, brand_id, b"<html><body><a href='/x'>Products</a> Contact us</body></html>")
    assert q(runtime_conn, "SELECT COUNT(*) FROM page_text WHERE page_id = %s", (page_id,))[0][0] == 1  # text kept
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("skipped",),)


def test_index_page_reads_a_pdf_into_one_row_per_page(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    pdf = make_pdf([[f"Model {MODEL}", "SEER2 15.2", "Tonnage 2.0"], ["Page two filler"]])
    page_id = make_page(runtime_conn, brand_id, pdf, is_pdf=True)
    rows = q(runtime_conn, "SELECT page_number, body FROM page_text WHERE page_id = %s ORDER BY page_number", (page_id,))
    assert [r[0] for r in rows] == [1, 2]
    assert MODEL in rows[0][1] and "Page two filler" in rows[1][1]
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("waiting",),)


def test_indexing_the_same_bytes_twice_gives_the_same_hashes_and_no_extra_rows(runtime_conn):
    from crawler.textextract import index_page
    name, brand_id = new_brand(runtime_conn)
    body = spec_html(MODEL)
    page_id = make_page(runtime_conn, brand_id, body)
    first = q(runtime_conn, "SELECT page_number, text_sha FROM page_text WHERE page_id = %s ORDER BY page_number", (page_id,))
    index_page(runtime_conn, page_id, body, is_pdf=False)
    second = q(runtime_conn, "SELECT page_number, text_sha FROM page_text WHERE page_id = %s ORDER BY page_number", (page_id,))
    assert first and first == second


def test_indexing_changed_content_updates_the_text_in_place(runtime_conn):
    from crawler.textextract import index_page
    name, brand_id = new_brand(runtime_conn)
    page_id = make_page(runtime_conn, brand_id, spec_html(MODEL, seer2="15.2"))
    old = q(runtime_conn, "SELECT text_sha FROM page_text WHERE page_id = %s AND page_number = 1", (page_id,))[0][0]
    index_page(runtime_conn, page_id, spec_html(MODEL, seer2="16.0"), is_pdf=False)
    rows = q(runtime_conn, "SELECT text_sha, body FROM page_text WHERE page_id = %s AND page_number = 1", (page_id,))
    assert len(rows) == 1
    assert rows[0][0] != old and "16.0" in rows[0][1]


def test_the_page_limit_reads_the_longest_ruud_manuals_whole():
    # 2026-10-29: docs 799, 800 and 604 (251, 255 and 221 pages) were cut at 200
    from crawler.textextract import PDF_MAX_PAGES
    assert PDF_MAX_PAGES >= 255
