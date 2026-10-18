"""Reads a PDF from stdin and prints its page texts as JSON. Run only as a child process with limits."""
import io
import json
import sys


MAX_PAGE_CHARS = 50_000


def page_text(page):
    """Text outside ruled tables, then each ruled table as tab-separated rows (empty cells kept)."""
    try:
        tables = page.find_tables()
        if not tables:
            return page.extract_text() or ""
        boxes = [t.bbox for t in tables]

        def outside(obj):
            return not any(obj["x0"] >= x0 - 1 and obj["x1"] <= x1 + 1 and obj["top"] >= top - 1
                           and obj["bottom"] <= bottom + 1 for x0, top, x1, bottom in boxes)

        parts = [page.filter(outside).extract_text() or ""]
        for table in tables:
            parts.append("\n".join("\t".join((cell or "").replace("\n", " ") for cell in row)
                                   for row in table.extract()))
        return "\n".join(parts)
    except Exception:
        return page.extract_text() or ""


def main():
    max_pages = int(sys.argv[1])
    import pdfplumber

    data = sys.stdin.buffer.read()
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            pages.append(page_text(page)[:MAX_PAGE_CHARS])
    sys.stdout.write(json.dumps({"pages": pages}))


if __name__ == "__main__":
    main()
