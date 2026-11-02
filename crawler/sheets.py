"""Spreadsheets uploaded on the admin page (EQUIPMENT_FILE_UPLOADS.md phase 2: D4, D6, D8).

- read_sheets: the text of an .xlsx or .csv, one page per sheet, read in a child process with a time and memory limit
  like a PDF (crawler/sheetchild.py says how a sheet becomes lines).
- price_sheet: a file whose name has the word price, pricing or quoter is one supplier's prices, shown to every shop
  and soon stale (D4, D8): refused.
- sheet_sections: a sheet's rows under the sheet's name. A long sheet is split only between rows, at the sectioner's
  MAX_WORDS, so every row stays whole and a model-number question finds its row.
"""
import re

from crawler.sections import MAX_WORDS, Section, _flagged, _words
from crawler.sheetchild import SHEET
from crawler.textextract import ExtractError, Extracted, _run_child

SHEET_TYPES = ("xlsx", "csv")
SHEET_TIMEOUT_SECONDS = 120
SHEET_MEMORY_BYTES = 1024 ** 3
_PRICE_SHEET = re.compile(r"(?<![A-Za-z])(prices?|pricing|quoter)(?![A-Za-z])", re.IGNORECASE)


def price_sheet(file_name):
    return bool(_PRICE_SHEET.search(file_name or ""))


def read_sheets(data, file_type):
    """Extracted with one page per sheet that holds anything. Raises ExtractError when it cannot be read."""
    try:
        raw = _run_child(data, [file_type], SHEET_TIMEOUT_SECONDS, SHEET_MEMORY_BYTES, child="sheetchild.py")
    except ExtractError as exc:  # the shared runner speaks of a PDF
        raise ExtractError(str(exc).replace("the PDF", "the spreadsheet"), exc.detail) from exc
    return Extracted(pages=[p for p in raw["pages"] if isinstance(p, str)], title=None)


def sheet_sections(pages):
    """pages: per sheet, its lines as layout items (kb.document_pages, from the original or the stored text)."""
    out = []
    for number, items in enumerate(pages, start=1):
        lines = [i["text"] for i in items if i.get("kind") == "text" and i["text"].strip()]
        path = lines.pop(0)[len(SHEET):].strip() if lines and lines[0].startswith(SHEET) else ""
        chunk, used = [], 0
        for line in lines:
            n = _words(line)
            if chunk and used + n > MAX_WORDS:
                out.append(Section(number, number, path, "table", "\n".join(chunk)))
                chunk, used = [], 0
            chunk.append(line)
            used += n
        if chunk:
            out.append(Section(number, number, path, "table", "\n".join(chunk)))
    return _flagged(out)
