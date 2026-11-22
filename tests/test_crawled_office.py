"""Spreadsheets (.xlsx) and Word files (.docx) found on brand sites are read and tagged like PDFs.

Prod 2026-11-21: 479 .xlsx and 339 .docx downloads were thrown away as bad_content_type, though the crawler reads both
for uploads (filereaders.py). Price lists are left out, as for uploads (sheets.price_sheet).
"""
import io

from fakeai import FakeAI
from p11support import HTML, crawl, serve, start, state, world  # noqa: F401  (world is a fixture)
from p3support import q, settle
from test_p11b_tagging import tags

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OCTET = "application/octet-stream"


def workbook():
    import openpyxl
    book = openpyxl.Workbook()
    book.active.append(["Model", "Tons", "SEER2"])
    book.active.append(["RA14AY24BJ1NA", 2, 15.2])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def word():
    import docx
    doc = docx.Document()
    doc.add_heading("RA14AY Installation Instructions", 1)
    doc.add_paragraph("Charge the RA14AY24BJ1NA to 8 degrees of subcooling.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def document_of(world, path):
    return q(world.conn, "SELECT d.file_type FROM documents d JOIN document_urls du ON du.document_id = d.id JOIN pages p "
                         "ON p.id = du.page_id WHERE p.brand_id = %s AND p.url = %s", (world.brand_id, world.url("ruud.test", path)))


def text_of(world, path):
    return " ".join(r[0] for r in q(world.conn, "SELECT t.body FROM page_text t JOIN pages p ON p.id = t.page_id "
                                                "WHERE p.brand_id = %s AND p.url = %s ORDER BY t.page_number",
                                    (world.brand_id, world.url("ruud.test", path))))


def test_a_spec_spreadsheet_on_a_brand_page_becomes_a_document_and_is_tagged(world):
    start(world)
    serve(world, "/start", ("/specs/RA14AY.xlsx", "Specifications"))
    world.site.route("/specs/RA14AY.xlsx", workbook(), headers={"Content-Type": XLSX})
    crawl(world)
    assert state(world, "/specs/RA14AY.xlsx")[0] == "fetched"
    assert "RA14AY24BJ1NA" in text_of(world, "/specs/RA14AY.xlsx")
    assert document_of(world, "/specs/RA14AY.xlsx") == (("xlsx",),)
    fake = FakeAI(lambda r: tags(doc_type="spec_sheet", models=("RA14AY24BJ1NA",)))
    settle(world.conn, fake, world.brand)
    assert len(fake.requests) == 1  # it reaches tagging like a PDF


def test_a_word_manual_sent_as_octet_stream_is_read_too(world):
    start(world)
    serve(world, "/start", ("/manuals/RA14AY.docx", "Installation instructions"))
    world.site.route("/manuals/RA14AY.docx", word(), headers={"Content-Type": OCTET})
    crawl(world)
    assert "8 degrees of subcooling" in text_of(world, "/manuals/RA14AY.docx")
    assert document_of(world, "/manuals/RA14AY.docx") == (("docx",),)


def test_a_price_list_spreadsheet_is_not_made_a_document(world):
    start(world)
    serve(world, "/start", ("/dealers/2026-price-list.xlsx", "Price list"))
    world.site.route("/dealers/2026-price-list.xlsx", workbook(), headers={"Content-Type": XLSX})
    crawl(world)
    assert document_of(world, "/dealers/2026-price-list.xlsx") == ()
    assert text_of(world, "/dealers/2026-price-list.xlsx") == ""


def test_a_file_named_xlsx_that_is_not_one_is_not_read(world):
    start(world)
    serve(world, "/start", ("/specs/broken.xlsx", "Specifications"))
    world.site.route("/specs/broken.xlsx", b"<html>not a spreadsheet</html>", headers={"Content-Type": OCTET})
    crawl(world)
    assert state(world, "/specs/broken.xlsx")[0] == "failed"
    assert document_of(world, "/specs/broken.xlsx") == ()
