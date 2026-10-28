"""Reads chosen PDF pages with OCR and prints them as JSON. Run only as a child process with limits.

`ocrchild.py <page,page,...>` with the PDF on stdin prints {"pages": {"<index>": [line, ...]}}, each line
{"text", "size", "top", "confidence"}: size is the line's height in points (comparable to a font size), top in points
from the top, confidence Tesseract's mean for the line's words (0-100).
Each page is drawn at DPI with pdfium and read by Tesseract, English only (user, 2026-10-28). Words Tesseract is
unsure of (confidence under MIN_CONFIDENCE) are left out, which drops most diagram debris.
"""
import io
import json
import statistics
import subprocess
import sys

DPI = 300
MIN_CONFIDENCE = 40


def read_page(page):
    image = page.render(scale=DPI / 72, grayscale=True).to_pil()
    png = io.BytesIO()
    image.save(png, "PNG")
    out = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "3", "tsv"], input=png.getvalue(),
                         capture_output=True, check=True, timeout=120).stdout.decode("utf-8", "replace")
    lines = {}
    for row in out.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        try:
            confidence = float(cols[10])
        except ValueError:
            continue
        if confidence < MIN_CONFIDENCE:
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))  # block, paragraph, line
        lines.setdefault(key, []).append((int(cols[6]), int(cols[7]), int(cols[9]), cols[11].strip(), confidence))
    result = []
    for key in sorted(lines):  # Tesseract's reading order: a column is read to its end before the next one
        words = sorted(lines[key])
        height = statistics.median(w[2] for w in words) * 72 / DPI
        result.append({"text": " ".join(w[3] for w in words), "size": round(height, 1),
                       "top": round(min(w[1] for w in words) * 72 / DPI, 1),
                       "confidence": round(statistics.mean(w[4] for w in words))})
    return result


def main():
    import pypdfium2 as pdfium
    wanted = [int(n) for n in sys.argv[1].split(",") if n]
    pdf = pdfium.PdfDocument(sys.stdin.buffer.read())
    pages = {}
    for n in wanted:
        if 0 <= n < len(pdf):
            page = pdf[n]
            pages[str(n)] = read_page(page)
            page.close()
    sys.stdout.write(json.dumps({"pages": pages}))


def _limit_memory():
    """The memory limit the parent asks for (CHILD_MEMORY_BYTES), set before anything big is loaded."""
    import os
    import resource
    limit = int(os.environ.get("CHILD_MEMORY_BYTES") or 0)
    if limit:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


if __name__ == "__main__":
    _limit_memory()
    main()
