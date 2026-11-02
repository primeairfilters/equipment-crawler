"""EQUIPMENT_FILE_UPLOADS.md phases 3 and 4: Word (.docx), PowerPoint (.pptx, with speaker notes, D10), saved web pages
(.html, .mht) and photos (.jpg, .png, read by Tesseract, D7) uploaded on the admin page.

Word keeps its headings and tables, so the sectioner cuts it like a PDF. A slide's speaker notes are read with that
slide (training decks keep the explanation there). A saved page is read like a crawled one. A photo's printed words
are read with OCR; a photo with no readable words is not added, with that reason.

Local library in a tmp folder (DOCS_DIR, inbox/ inside it), fake AI, `_testing`, one rolled-back transaction per test."""
import hashlib
import io
import uuid

import pytest

from p11support import world  # noqa: F401  (world is a fixture)
from p3support import q
from test_auto_run import auto
from test_uploads import discovered, outcome, process, queue


def word(code):
    import docx
    doc = docx.Document()
    doc.add_heading("Refrigerant charging", level=1)
    doc.add_paragraph(f"Charge the {code} by subcooling. Weigh in the factory charge first, then check the "
                      "subcooling at the service valve after fifteen minutes of run time.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Line set", "Charge adder"
    table.cell(1, 0).text, table.cell(1, 1).text = "25 ft", "0.6 oz per ft"
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def deck(code):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    one = prs.slides.add_slide(prs.slide_layouts[1])
    one.shapes.title.text = "Board fault codes"
    one.placeholders[1].text = f"Flash code 3 on the {code}"
    one.notes_slide.notes_text_frame.text = "Code 3 means the pressure switch never closed: check the hose for water."
    hidden = prs.slides.add_slide(prs.slide_layouts[1])
    hidden.shapes.title.text = "HIDDENSLIDE draft"
    hidden._element.set("show", "0")
    two = prs.slides.add_slide(prs.slide_layouts[5])
    two.shapes.title.text = "Airflow"
    rows = two.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    rows.cell(0, 0).text, rows.cell(0, 1).text = "Tons", "CFM"
    rows.cell(1, 0).text, rows.cell(1, 1).text = "3", "1200"
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def saved_page(code):
    return (f"<html><head><title>Service note {code}</title><script>var SCRIPTWORD = 1;</script></head><body>"
            f"<h1>Service note</h1><p>On the {code} replace the inducer gasket when the motor is changed.</p>"
            "</body></html>").encode()


def mht(code):
    html = saved_page(code).decode().replace("=", "=3D")
    return ("From: <Saved by Blink>\r\nSubject: Service note\r\nMIME-Version: 1.0\r\n"
            "Content-Type: multipart/related; type=\"text/html\"; boundary=\"----B\"\r\n\r\n"
            "------B\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\n"
            f"Content-Location: https://example.test/note\r\n\r\n{html}\r\n------B--\r\n").encode()


def photo(text, fmt="PNG"):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1400, 300), "white")
    if text:
        ImageDraw.Draw(image).text((40, 100), text, fill="black", font=ImageFont.load_default(size=56))
    out = io.BytesIO()
    image.save(out, fmt)
    return out.getvalue()


def code():
    """A model number OCR reads the same every time: no 0/O, 1/I, 8/B look-alikes, and no 9 (the test font draws it so it reads as Q)."""
    import random
    return "RA" + "".join(random.choice("ACEFHKMNPRTUVWXY234567") for _ in range(6)) + "7JA"


def page_text(conn, upload):
    return [b for (b,) in q(conn, "SELECT t.body FROM page_text t JOIN control.uploads u ON u.page_id = t.page_id "
                                  "WHERE u.id = %s ORDER BY t.page_number", (upload,))]


# ---------------------------------------------------------------- reading

def test_a_word_file_is_read_with_its_heading_text_and_table(world):
    c = code()
    upload = queue(world.conn, word(c), name="Charging note.docx", brand=world.brand)
    assert process(world.conn)["added"] == 1
    (body,) = page_text(world.conn, upload)
    assert "Refrigerant charging" in body and f"Charge the {c} by subcooling" in body
    assert "25 ft | 0.6 oz per ft" in body


def test_a_slide_is_read_with_its_speaker_notes_and_a_hidden_slide_is_left_out(world):
    c = code()
    upload = queue(world.conn, deck(c), name="Training.pptx", brand=world.brand)
    process(world.conn)
    pages = page_text(world.conn, upload)
    assert len(pages) == 2
    assert pages[0].splitlines()[0] == "Board fault codes"
    assert f"Flash code 3 on the {c}" in pages[0]
    assert "Notes: Code 3 means the pressure switch never closed: check the hose for water." in pages[0]
    assert "Tons | CFM" in pages[1] and "3 | 1200" in pages[1]
    assert not any("HIDDENSLIDE" in p for p in pages)


@pytest.mark.parametrize("make, name, file_type", [(saved_page, "Service note.html", "html"),
                                                   (mht, "Service note.mht", "mht")])
def test_a_saved_web_page_is_read_like_a_crawled_one(world, make, name, file_type):
    from crawler import library
    c = code()
    body = make(c)
    upload = queue(world.conn, body, name=name, brand=world.brand)
    process(world.conn)
    (text,) = page_text(world.conn, upload)
    assert f"On the {c} replace the inducer gasket" in text and "SCRIPTWORD" not in text
    document_id = outcome(world.conn, upload)[2]
    assert q(world.conn, "SELECT file_type FROM documents WHERE id = %s", (document_id,))[0][0] == file_type
    assert library.has_file(hashlib.sha256(body).hexdigest(), file_type)


@pytest.mark.parametrize("fmt, name, file_type", [("PNG", "label.png", "png"), ("JPEG", "label.jpeg", "jpg")])
def test_a_photo_of_a_printed_label_becomes_a_document(world, fmt, name, file_type):
    c = code()
    upload = queue(world.conn, photo(f"MODEL {c} 208/230 VOLTS", fmt), name=name, brand=world.brand)
    process(world.conn)
    state, reason, document_id, _ = outcome(world.conn, upload)
    assert (state, reason) == ("added", None)
    (text,) = page_text(world.conn, upload)
    assert c in text.replace(" ", "")
    assert q(world.conn, "SELECT file_type, needs_ocr FROM documents WHERE id = %s", (document_id,))[0] == (file_type, False)


def test_a_photo_with_no_readable_words_is_not_added(world):
    upload = queue(world.conn, photo(None), name="walkaround.jpg")
    process(world.conn)
    assert outcome(world.conn, upload)[:3] == ("rejected", "no readable words in it", None)


@pytest.mark.parametrize("name", ["Old note.doc", "Old deck.ppt"])
def test_the_old_office_formats_are_not_read(world, name):
    upload = queue(world.conn, b"\xd0\xcf\x11\xe0 an old office file", name=name)
    process(world.conn)
    assert outcome(world.conn, upload)[:2] == ("rejected", "not a file Tracforce reads")


def test_a_file_named_docx_that_is_not_one_is_rejected_with_the_reason(world):
    upload = queue(world.conn, b"PK\x03\x04" + uuid.uuid4().bytes, name="Broken.docx")
    process(world.conn)
    state, reason, _, _ = outcome(world.conn, upload)
    assert state == "rejected" and "could not be read" in reason


# ---------------------------------------------------------------- sections, reread, search (done checks)

def test_a_word_file_is_cut_by_its_headings(world):
    from crawler import kb
    upload = queue(world.conn, word(code()), name="Charging note.docx", brand=world.brand)
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    q(world.conn, "UPDATE documents SET state = 'tagged', doc_type = 'service_manual' WHERE id = %s", (document_id,))
    q(world.conn, "UPDATE page_text SET body = 'stale' WHERE page_id = (SELECT page_id FROM control.uploads WHERE id = %s)",
      (upload,))  # so a heading in the sections proves the original was read
    assert kb.build(world.conn, document_id) == 1
    sections = q(world.conn, "SELECT heading_path, kind, body FROM sections WHERE document_id = %s ORDER BY section_no",
                 (document_id,))
    assert any(path == "Refrigerant charging" and "subcooling" in body for path, _, body in sections)
    assert any(kind == "table" and "0.6 oz per ft" in body for _, kind, body in sections)


def test_reread_reads_a_deck_as_a_deck(world):
    from crawler.documents import reread
    c = code()
    upload = queue(world.conn, deck(c), name="Training.pptx", brand=world.brand)
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    q(world.conn, "UPDATE page_text SET body = 'stale' WHERE page_id = (SELECT page_id FROM control.uploads WHERE id = %s)",
      (upload,))
    assert reread(world.conn, documents=[document_id])["read"] == 1
    assert f"Flash code 3 on the {c}" in page_text(world.conn, upload)[0]


@pytest.mark.parametrize("make, name", [(word, "Charging note.docx"), (deck, "Training.pptx"),
                                        (saved_page, "Service note.html"),
                                        (lambda c: photo(f"MODEL {c} 208/230 VOLTS"), "label.png")])
def test_each_kind_of_file_is_tagged_live_and_a_search_finds_its_text(world, make, name):
    from crawler.search import search
    from test_p14_search import BagEmbedder
    discovered(world)
    c = code()
    upload = queue(world.conn, make(c), name=name, brand=world.brand)
    auto(world)
    document_id = outcome(world.conn, upload)[2]
    assert q(world.conn, "SELECT state FROM documents WHERE id = %s", (document_id,))[0][0] == "live"
    question = f"What about the {c}?"
    hits = search(world.conn, question, BagEmbedder().embed([question])[0][0])
    assert any(h.document_id == document_id and c in h.body.replace(" ", "") for h in hits)
