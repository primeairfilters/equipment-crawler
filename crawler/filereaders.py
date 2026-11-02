"""Every kind of uploaded file other than a PDF (EQUIPMENT_FILE_UPLOADS.md phases 2-4): which type a file is, its text
(for page_text and tagging) and its layout (for sections).

- Spreadsheets (.xlsx, .csv): crawler/sheets.py.
- Word (.docx), PowerPoint (.pptx) and photos (.jpg, .png): crawler/docchild.py, in a child process with a time and
  memory limit like a PDF. A photo is read by Tesseract (D7).
- Saved web pages (.html, .mht): read like a crawled page (textextract.extract_html); an .mht is a web page packed in
  a MIME message, and its first HTML part is the page.

A PDF is not here: it keeps its own readers (textextract.py), so nothing about PDFs changes.
"""
import email
from email import policy

from crawler.sheets import SHEET_TYPES, read_sheets
from crawler.textextract import ExtractError, Extracted, _run_child, extract_html, has_text

DOC_TYPES = ("docx", "pptx")
PAGE_TYPES = ("html", "mht")
PHOTO_TYPES = ("jpg", "png")
CHILD_TIMEOUT_SECONDS = 180  # a photo's OCR takes 5-20 s; a big deck's text a few seconds
CHILD_MEMORY_BYTES = 1024 ** 3

_BY_NAME = {"xlsx": "xlsx", "csv": "csv", "docx": "docx", "pptx": "pptx", "html": "html", "htm": "html", "mht": "mht",
            "mhtml": "mht", "jpg": "jpg", "jpeg": "jpg", "png": "png"}
_ZIPPED = ("xlsx", "docx", "pptx")


def file_type(body, name):
    """pdf by its content (any name); the others by their name, the Office ones also by their zip header. None for a
    file it does not read (the old .xls, .doc and .ppt among them)."""
    if body.startswith(b"%PDF-"):
        return "pdf"
    found = _BY_NAME.get(name.rsplit(".", 1)[-1].lower() if "." in name else "")
    if found in _ZIPPED and not body.startswith(b"PK\x03\x04"):
        return None
    return found


def nothing_to_read(found, kind):
    """The reason an upload that read without an error is still not added, or None."""
    if kind in PHOTO_TYPES:
        return None if any(has_text(p) for p in found.pages) else "no readable words in it"
    return None if any(p.strip() for p in found.pages) else "it is empty"


def _child(data, kind):
    try:
        return _run_child(data, [kind], CHILD_TIMEOUT_SECONDS, CHILD_MEMORY_BYTES, child="docchild.py")
    except ExtractError as exc:  # the shared runner speaks of a PDF
        raise ExtractError(str(exc).replace("the PDF", "the file"), exc.detail) from exc


def _web_page(data, kind):
    if kind == "html":
        return extract_html(data)
    message = email.message_from_bytes(data, policy=policy.default)
    for part in message.walk():
        if part.get_content_type() == "text/html":
            return extract_html(part.get_content().encode("utf-8"))
    raise ExtractError("there is no web page in the saved file")


def extract(data, kind):
    """The file's text, one page per sheet, slide or document (textextract.Extracted). Raises ExtractError."""
    if kind in SHEET_TYPES:
        return read_sheets(data, kind)
    if kind in PAGE_TYPES:
        return _web_page(data, kind)
    raw = _child(data, kind)
    return Extracted(pages=[p for p in raw["pages"] if isinstance(p, str)], title=raw.get("title"))


def layout(data, kind):
    """Layout items per page for the sectioner (kb.read_original). Raises ExtractError."""
    if kind in SHEET_TYPES or kind in PAGE_TYPES:
        return [[{"kind": "text", "text": line, "size": 0, "bold": False} for line in text.splitlines() if line.strip()]
                for text in extract(data, kind).pages]
    return _child(data, kind)["layout"]
