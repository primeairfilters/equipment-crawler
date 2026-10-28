"""OCR (2026-10-28, user: English only): PDF pages with no text layer (scanned, or printed as outlines) are read with
Tesseract. Only pages with no text are read; a page that reads as Spanish or French is left empty; results are kept in
the library so a page is read once; sections from such pages are flagged `ocr` (OCR misreads digits). Real Tesseract,
on pictures of text made with Pillow."""
import hashlib
import io

import pytest

from p11support import crawl, serve, serve_pdf, start, world  # noqa: F401  (world is a fixture)
from p3support import make_pdf

BODY = ("Check the subcooling at the liquid line before you add refrigerant. The service valves must be open and "
        "the system must run for fifteen minutes before you read the pressures on the gauges.")
FRENCH = ("Vérifier le sous-refroidissement de la ligne liquide avant d'ajouter le réfrigérant. Les vannes de service "
          "doivent être ouvertes et le système doit fonctionner pendant quinze minutes avant la lecture des pressions.")


def picture_page(heading, body):
    """A letter-size page that is only a picture of text, 150 dpi, like a scanned or outlined page."""
    from PIL import Image, ImageDraw, ImageFont
    page = Image.new("L", (1275, 1650), 255)
    draw = ImageDraw.Draw(page)
    draw.text((110, 140), heading, font=ImageFont.load_default(size=56), fill=0)
    font, y, line = ImageFont.load_default(size=30), 260, ""
    for word in body.split():
        if draw.textlength(line + " " + word, font=font) > 1050:
            draw.text((110, y), line.strip(), font=font, fill=0)
            y, line = y + 46, ""
        line += " " + word
    draw.text((110, y), line.strip(), font=font, fill=0)
    return page


def picture_pdf(*pages):
    out = io.BytesIO()
    pages[0].save(out, "PDF", resolution=150, save_all=True, append_images=list(pages[1:]))
    return out.getvalue()


def text_then_picture():
    """Page 1 has a text layer, page 2 is a picture: the usual case, an IOM with some pages printed as outlines."""
    import pypdfium2 as pdfium
    merged = pdfium.PdfDocument.new()
    for part in (make_pdf([["Installation Manual", "Read all instructions before installing the unit."]]),
                 picture_pdf(picture_page("Refrigerant Charging", BODY))):
        merged.import_pages(pdfium.PdfDocument(part))
    out = io.BytesIO()
    merged.save(out)
    return out.getvalue()


def test_a_page_with_no_text_layer_is_read_with_ocr():
    from crawler.textextract import extract_pdf
    found = extract_pdf(picture_pdf(picture_page("Refrigerant Charging", BODY)))
    assert "subcooling at the liquid line" in found.pages[0] and found.ocr_pages == {0}


def test_only_the_pages_with_no_text_are_read_with_ocr():
    from crawler.textextract import extract_pdf
    found = extract_pdf(text_then_picture())
    assert "Read all instructions" in found.pages[0] and "subcooling" in found.pages[1] and found.ocr_pages == {1}


def test_a_pdf_with_a_text_layer_never_starts_ocr(monkeypatch):
    import crawler.textextract

    def no_ocr(*args, **kwargs):
        raise AssertionError("OCR started on a page that has text")

    monkeypatch.setattr(crawler.textextract, "_ocr_child", no_ocr)
    found = crawler.textextract.extract_pdf(make_pdf([["Installation Manual", "Read all instructions first."]]))
    assert found.ocr_pages == set() and "Read all instructions" in found.pages[0]


def test_a_page_that_reads_as_french_or_spanish_is_left_empty():
    from crawler.textextract import extract_pdf
    found = extract_pdf(picture_pdf(picture_page("Charge du réfrigérant", FRENCH)))
    assert found.pages == [""] and found.ocr_pages == set()


def test_ocr_pages_give_layout_lines_and_their_sections_are_flagged_ocr():
    from crawler.sections import build_sections
    from crawler.textextract import extract_layout
    pages = extract_layout(text_then_picture())
    assert any(i.get("ocr") for i in pages[1]) and not any(i.get("ocr") for i in pages[0])
    sections = build_sections(pages)
    ocr = [s for s in sections if "subcooling" in s.body]
    assert ocr and all("ocr" in s.flags for s in ocr)
    assert ocr[0].heading_path == "Refrigerant Charging"  # the bigger OCR line is still a heading


def test_a_page_is_read_with_ocr_once_and_the_result_is_kept_in_the_library(tmp_path, monkeypatch):
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    pdf = picture_pdf(picture_page("Refrigerant Charging", BODY))
    first = crawler.textextract.extract_pdf(pdf)

    def no_ocr(*args, **kwargs):
        raise AssertionError("the same page was read with OCR twice")

    monkeypatch.setattr(crawler.textextract, "_ocr_child", no_ocr)
    assert crawler.textextract.extract_pdf(pdf).pages == first.pages
    assert any(crawler.textextract.extract_layout(pdf)[0])


def test_without_tesseract_a_page_with_no_text_stays_empty(monkeypatch):
    import shutil
    import crawler.textextract
    monkeypatch.setattr(shutil, "which", lambda name: None)
    found = crawler.textextract.extract_pdf(picture_pdf(picture_page("Refrigerant Charging", BODY)))
    assert found.pages == [""] and found.ocr_pages == set()


def test_reread_fills_a_scanned_manual_and_it_no_longer_needs_ocr(world, monkeypatch):
    import shutil
    from crawler.documents import reread
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None)  # crawled before OCR was there
    start(world)
    serve(world, "/start", "/scan.pdf")
    body = picture_pdf(picture_page("Refrigerant Charging", BODY))
    serve_pdf(world, "/scan.pdf", body)
    crawl(world)
    sha = hashlib.sha256(body).hexdigest()
    assert world.one("SELECT text_pages, needs_ocr FROM documents WHERE content_sha = %s", (sha,)) == ((0, True),)
    monkeypatch.setattr(shutil, "which", real_which)
    assert reread(world.conn)["read"] == 1
    assert world.one("SELECT page_count, text_pages, needs_ocr FROM documents WHERE content_sha = %s",
                     (sha,)) == ((1, 1, False),)


DUCTS = ("Every foot of flexible ducting counts as three feet of rigid ducting. Ducting must be insulated to keep "
         "condensation off the outside of the duct and away from the ceiling below the heater.")


def two_column_page(left, right):
    """A picture page with two columns of text side by side, like a water heater manual."""
    from PIL import Image, ImageDraw, ImageFont
    page = Image.new("L", (1275, 1650), 255)
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=30)
    for x, text in ((90, left), (680, right)):
        y, line = 200, ""
        for word in text.split():
            if draw.textlength(line + " " + word, font=font) > 500:
                draw.text((x, y), line.strip(), font=font, fill=0)
                y, line = y + 46, ""
            line += " " + word
        draw.text((x, y), line.strip(), font=font, fill=0)
    return page


def test_a_two_column_page_is_read_one_column_at_a_time():
    from crawler.textextract import extract_pdf
    text = extract_pdf(picture_pdf(two_column_page(BODY, DUCTS))).pages[0]
    assert "subcooling" in text and "flexible ducting" in text
    assert text.index("gauges") < text.index("Every foot")  # the whole left column comes before the right one


def test_a_page_holding_only_a_table_keeps_its_table_and_is_not_read_with_ocr(monkeypatch):
    import crawler.textextract

    def no_ocr(*args, **kwargs):
        raise AssertionError("OCR started on a page that has a table")

    table = {"kind": "table", "rows": [["GENERAL DATA", "RD18AY24"], ["Nominal Tonnage", "2"]]}
    monkeypatch.setattr(crawler.textextract, "_run_child", lambda *a, **k: {"layout": [[table]], "rows": None})
    monkeypatch.setattr(crawler.textextract, "_ocr_child", no_ocr)
    pages = crawler.textextract.extract_layout(b"%PDF")
    assert pages == [[{"kind": "table", "rows": [["GENERAL DATA", "RD18AY24"], ["Nominal Tonnage", "2"]]}]]


# OCR headings (user, 2026-10-28: "clean the headings"). Real cases from the Ruud OCR run: warning icons read as letters
# before the caption, diagram labels, and big-font sentences became headings.
FILLER = ("Connect the gauge manifold to the service ports and run the system for fifteen minutes before reading the "
          "pressures. ") * 18  # over MIN_WORDS, so a section never merges into the next
def cover():
    """Page 1: body text only, worded unlike the rest (a line on two pages is dropped as a running header)."""
    return [ocr_line(BODY * 20, size=10.0)]


def ocr_line(text, size=14.0, confidence=95):
    return {"kind": "text", "text": text, "size": size, "bold": False, "ocr": True, "confidence": confidence}


def headings_of(*titles):
    """The heading paths build_sections makes when each title (an OCR line in a big font) is followed by body text, on
    page 2 (page 1 holds only body text, so no title is taken for a cover title)."""
    from crawler.sections import build_sections
    page = []
    for title in titles:
        page += [ocr_line(title), ocr_line(FILLER, size=10.0)]
    return {s.heading_path for s in build_sections([cover(), page])}


@pytest.mark.parametrize("junk", ["A DANGER", "ADANGER!", "A WARNING!", "Z\\WARNING", "/\\ WARNING", "Ay A2L",
                                  "Ba CAUTION", "M& ~~ CAUTION", "4& ~~ CAUTION", "A. CAUTION", "(LL CAUTION",
                                  "ADANGER: Before manually", "A2L WARNING If the refrigerant is leaked"])
def test_a_warning_icon_read_as_letters_before_a_caption_is_no_heading(junk):
    paths = headings_of("Refrigerant Charging", junk)
    assert not any(junk in p for p in paths) and any("Refrigerant Charging" in p for p in paths)


@pytest.mark.parametrize("junk", ["60in.(1500mm)", "_HLH-018TO", "<Example> (For MSP/HSP Type)", "SMART MOde 5",
                                  "Wiring Diagram.......... 36", "(field-supplied)", "[Recovery",
                                  "If the refrigerant is leaked and exposed to an",
                                  "Thank you very much for purchasing this Heat Pump Air Conditioner",
                                  "Conditional Conditional Conditional", "Number Tool Number Tool Number Tool",
                                  "Once connected, prod-", "50mm Approx", "/ Nut", "—Fiberglass Insulation",
                                  "& Sleep 4 indicator"])
def test_diagram_labels_and_big_font_sentences_read_with_ocr_are_no_headings(junk):
    paths = headings_of("Refrigerant Charging", junk)
    assert not any(junk in p for p in paths) and any("Refrigerant Charging" in p for p in paths)


def test_an_ocr_line_tesseract_is_unsure_of_is_no_heading():
    from crawler.sections import build_sections
    page = [ocr_line("Refrigerant Charging"), ocr_line(FILLER, size=10.0), ocr_line("SSSA", confidence=66),
            ocr_line(FILLER, size=10.0)]
    assert {s.heading_path for s in build_sections([cover(), page])} == {"", "Refrigerant Charging"}


@pytest.mark.parametrize("title", ["4.1 Pipe Material", "(1) Pipe cutting", "Caution Statements", "E. ELECTRICAL",
                                   "Installing the water heater", "Precautions for using R454B refrigerant"])
def test_real_ocr_headings_stay_headings(title):
    assert any(title in p for p in headings_of("Refrigerant Charging", title))


def test_a_ruling_read_as_a_bar_is_trimmed_from_an_ocr_line(monkeypatch):
    import crawler.textextract
    monkeypatch.setattr(crawler.textextract, "_run_child", lambda *a, **k: {"layout": [[]], "rows": None})
    monkeypatch.setattr(crawler.textextract, "ocr_pages",
                        lambda *a, **k: {0: [{"text": "| Wiring |", "size": 14, "confidence": 95}]})
    assert crawler.textextract.extract_layout(b"%PDF")[0][0]["text"] == "Wiring"


# ---------------------------------------------------------------- pages that are a drawing with a caption (user, 2026-10-28)

def caption_over_picture(caption, picture):
    """A page whose text layer is only a caption, over a picture of text (a wiring diagram, a dimension drawing)."""
    import ctypes
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    out = pdfium.PdfDocument.new()
    page = out.new_page(612, 792)
    page.insert_obj(pdfium.PdfDocument(picture).page_as_xobject(0, out).as_pageobject())
    text = raw.FPDFPageObj_NewTextObj(out.raw, b"Helvetica", ctypes.c_float(18))
    wide = (caption + "\0").encode("utf-16-le")
    raw.FPDFText_SetText(text, ctypes.cast(ctypes.c_char_p(wide), ctypes.POINTER(raw.FPDF_WCHAR)))
    raw.FPDFPageObj_Transform(text, 1, 0, 0, 1, 72, 750)
    raw.FPDFPage_InsertObject(page.raw, text)
    page.gen_content()
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


def test_a_drawing_page_with_only_a_caption_is_read_with_ocr_and_keeps_its_caption():
    from crawler.textextract import extract_layout, extract_pdf
    pdf = caption_over_picture("9. WIRING DIAGRAM", picture_pdf(picture_page("", BODY)))
    found = extract_pdf(pdf)
    assert "WIRING DIAGRAM" in found.pages[0] and "subcooling" in found.pages[0] and found.ocr_pages == {0}
    items = extract_layout(pdf)[0]
    assert items[0]["text"] == "9. WIRING DIAGRAM" and not items[0].get("ocr")
    assert any(i.get("ocr") and "subcooling" in i["text"] for i in items)


@pytest.mark.parametrize("caption", ["Notes", "37", "Intentionally left blank", "ESPAÑOL", "FRANÇAIS 17", "Page 4"])
def test_a_notes_blank_number_or_language_page_is_not_read_with_ocr(caption, monkeypatch):
    import crawler.textextract

    def no_ocr(*args, **kwargs):
        raise AssertionError("OCR started on a page that is not a captioned drawing")

    monkeypatch.setattr(crawler.textextract, "_ocr_child", no_ocr)
    pdf = caption_over_picture(caption, picture_pdf(picture_page("", BODY)))
    assert crawler.textextract.extract_pdf(pdf).ocr_pages == set()
    crawler.textextract.extract_layout(pdf)
