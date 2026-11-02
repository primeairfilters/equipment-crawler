"""Reads a Word file, a PowerPoint deck or a photo from stdin and prints its text as JSON. Run only as a child process
with limits (crawler/filereaders.py). EQUIPMENT_FILE_UPLOADS.md phases 3 and 4.

`docchild.py docx|pptx|jpg|png` prints {"pages": [text, ...], "layout": [[item, ...] per page], "title"}: the layout
items are the sectioner's ({"kind": "text", "text", "size", "bold"} or {"kind": "table", "rows"}), so a document is cut
by its headings like a PDF.

- Word: one page. A paragraph styled Title or Heading N is larger by its level; tables are tables.
- PowerPoint: a page per slide, hidden slides left out. The slide's title is its heading, then its text and tables
  in the order they sit on the slide, then its speaker notes ("Notes: ...", D10).
- A photo: one page, read by Tesseract like a scanned PDF page (ocrchild.read_page, so both read the same way), after
  turning it the way the camera held it. Only printed words come through (D7).
"""
import io
import json
import os
import sys

BODY = 11
HEADING_SIZES = {"title": 26, "heading 1": 20, "heading 2": 17, "heading 3": 15}
LOWER_HEADING = 13  # Heading 4 and below
SLIDE_TITLE = 24
MAX_PHOTO_SIDE = 4000  # pixels; a bigger photo is shrunk first (Tesseract time and memory)


def _clean(text):
    return " ".join((text or "").split())


def _rows(cells_by_row):
    rows = [[_clean(c) for c in row] for row in cells_by_row]
    return [r for r in rows if any(r)]


def _text_lines(items):
    lines = []
    for i in items:
        if i["kind"] == "table":
            lines += [" | ".join(r) for r in i["rows"]]
        else:
            lines.append(i["text"])
    return "\n".join(lines)


def _heading_size(style):
    name = (style or "").lower()
    if name in HEADING_SIZES:
        return HEADING_SIZES[name]
    return LOWER_HEADING if name.startswith("heading ") else BODY


def read_docx(data):
    import docx
    from docx.table import Table
    doc = docx.Document(io.BytesIO(data))
    items = []
    for block in doc.iter_inner_content():
        if isinstance(block, Table):
            rows = _rows([[c.text for c in row.cells] for row in block.rows])
            if rows:
                items.append({"kind": "table", "rows": rows})
            continue
        text = _clean(block.text)
        if not text:
            continue
        runs = [r for r in block.runs if r.text.strip()]
        items.append({"kind": "text", "text": text,
                      "size": _heading_size(block.style.name if block.style is not None else ""),
                      "bold": bool(runs) and all(r.bold for r in runs)})
    title = _clean(doc.core_properties.title) or None
    return [items] if items else [], title


def _shapes(shapes):
    """Shapes in the order they sit on the slide (top, then left), groups opened."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    out = []
    for shape in sorted(shapes, key=lambda s: (s.top or 0, s.left or 0)):
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            out += _shapes(shape.shapes)
        else:
            out.append(shape)
    return out


def read_pptx(data):
    from pptx import Presentation
    deck = Presentation(io.BytesIO(data))
    pages = []
    for slide in deck.slides:
        if slide._element.get("show") == "0":  # a hidden slide
            continue
        items = []
        title = slide.shapes.title
        if title is not None and _clean(title.text):
            items.append({"kind": "text", "text": _clean(title.text), "size": SLIDE_TITLE, "bold": False})
        for shape in _shapes(slide.shapes):
            if title is not None and shape.shape_id == title.shape_id:
                continue
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = _rows([[c.text for c in row.cells] for row in shape.table.rows])
                if rows:
                    items.append({"kind": "table", "rows": rows})
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = _clean("".join(r.text for r in paragraph.runs))
                    if text:
                        items.append({"kind": "text", "text": text, "size": BODY, "bold": False})
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = [_clean(p.text) for p in slide.notes_slide.notes_text_frame.paragraphs]
            notes = [n for n in notes if n]
            for n, text in enumerate(notes):
                items.append({"kind": "text", "text": f"Notes: {text}" if n == 0 else text, "size": BODY, "bold": False})
        if items:
            pages.append(items)
    return pages, _clean(deck.core_properties.title) or None


class _Drawn:
    """A photo standing in for a pdfium page, so ocrchild.read_page reads it exactly as it reads a scanned page."""

    def __init__(self, image):
        self.image = image

    def render(self, **_):
        return self

    def to_pil(self):
        return self.image


def read_photo(data):
    from PIL import Image, ImageOps
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ocrchild import read_page
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
    if max(image.size) > MAX_PHOTO_SIDE:
        image.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
    items = [{"kind": "text", "text": line["text"], "size": line["size"], "bold": False, "ocr": True,
              "confidence": line["confidence"]} for line in read_page(_Drawn(image)) if _clean(line["text"])]
    return [items] if items else [], None


READERS = {"docx": read_docx, "pptx": read_pptx, "jpg": read_photo, "png": read_photo}


def main():
    layout, title = READERS[sys.argv[1]](sys.stdin.buffer.read())
    sys.stdout.write(json.dumps({"pages": [_text_lines(items) for items in layout], "layout": layout, "title": title}))


def _limit_memory():
    """The memory limit the parent asks for (CHILD_MEMORY_BYTES), set before anything big is loaded."""
    import resource
    limit = int(os.environ.get("CHILD_MEMORY_BYTES") or 0)
    if limit:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


if __name__ == "__main__":
    _limit_memory()
    main()
