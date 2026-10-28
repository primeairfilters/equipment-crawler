"""Text out of HTML and PDF, stored in page_text, with the free filter deciding what AI reads."""
import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

PDF_MAX_PAGES = 200
PDF_TIMEOUT_SECONDS = 180  # big Ruud manuals took 63-109 s to read (text, tables, drawn-text check); 60 s cut them off
LAYOUT_TIMEOUT_SECONDS = 180  # text and layout of up to 200 pages in one pass
PDF_MEMORY_BYTES = 1024 ** 3
PDF_MAX_OUTPUT_BYTES = 20 * 1024 * 1024
DRAWN_TEXT_SHARE = 0.2  # this share of a PDF's text lines drawn as shapes: its text layer is fragments, treat as a scan
DRAWN_TEXT_MIN_LINES = 5
OCR_VERSION = "3"  # results are kept in the library under this version; a new reader gets a new key (2: column order, 3: confidence)
OCR_SECONDS_BASE = 30
OCR_SECONDS_PER_PAGE = 20  # Tesseract at 300 dpi takes 2-5 s a page; the rest is headroom

_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
                            "ﬅ": "st", "ﬆ": "st", " ": " "})
_SKIP_TAGS = {"script", "style", "noscript", "template", "head", "svg", "nav", "footer"}
_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "br", "section", "article",
               "header", "footer", "table", "thead", "tbody", "tfoot", "pre", "blockquote", "hr", "form", "dl",
               "dt", "dd", "caption", "main", "nav", "aside", "figure", "fieldset", "address", "body", "html"}
_CELL_TAGS = {"td", "th"}


class ExtractError(Exception):
    pass


@dataclass
class Extracted:
    pages: list = field(default_factory=list)
    title: str = None
    drawn_text: bool = False  # text drawn as shapes: the pages with drawn lines are left empty (drawn_pages)
    ocr_pages: set = field(default_factory=set)  # page indexes whose text came from OCR


def clean(text):
    """Ligatures fixed, glyphs with no text map (`(cid:31)`) dropped, runs of spaces collapsed per line, blank lines dropped. Same words give the same text."""
    text = re.sub(r"\(cid:\d+\)", "", text.translate(_LIGATURES).replace("\x00", ""))  # a glyph with no text
    text = text.encode("utf-8", "ignore").decode("utf-8")
    lines = []
    for line in text.splitlines():
        cells = [re.sub(r"[ \f\v\r]+", " ", cell).strip() for cell in line.split("\t")]
        line = "\t".join(cells).strip("\t ")
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


class _EndOfBlock:
    pass


_END = _EndOfBlock()


def extract_html(body):
    try:
        tree = LexborHTMLParser(body)
        title_node = tree.css_first("title")
        title = clean(title_node.text()) if title_node is not None else None
        out = []
        stack = [tree.body or tree.root]
        while stack:
            node = stack.pop()
            if node is _END:
                out.append("\n")
                continue
            tag = node.tag
            if tag == "-text":
                out.append(re.sub(r"\s+", " ", node.text_content or ""))
                continue
            if tag in _SKIP_TAGS or tag.startswith(("-", "!")):
                continue
            if tag == "tr":
                cells = [c.text(deep=True, separator=" ", strip=True) for c in _children(node) if c.tag in _CELL_TAGS]
                out.append("\n" + "\t".join(re.sub(r"\s+", " ", c) for c in cells) + "\n")
                continue
            if tag in _BLOCK_TAGS:
                out.append("\n")
                stack.append(_END)
            stack.extend(reversed(list(_children(node))))
        text = clean("".join(out))
    except Exception as exc:  # hostile markup must not stop a run
        raise ExtractError(f"could not read the page: {exc}") from exc
    return Extracted(pages=[text] if text else [], title=title or None)


def _children(node):
    child = node.child
    while child is not None:
        yield child
        child = child.next


def extract_layout(data, max_pages=PDF_MAX_PAGES, timeout=LAYOUT_TIMEOUT_SECONDS, memory=PDF_MEMORY_BYTES):
    """Per page, the text lines with font size and boldness, and the ruled tables, in reading order (for sections).
    The reader's answer is kept in the library (_kept_layout), so a sectioner change does not read every PDF again."""
    raw = _kept_layout(data, max_pages)
    if raw is None:
        raw = _run_child(data, [str(max_pages), "layout"], timeout, memory)
        _keep_layout(data, max_pages, raw)
    try:
        layout = raw["layout"]
    except KeyError as exc:
        raise ExtractError("the PDF reader gave an unusable answer") from exc
    blank = drawn_pages(raw.get("rows"))
    pages = []
    for number, page in enumerate(layout[:max_pages]):
        items = []
        if number in blank:
            pages.append(items)
            continue
        for item in page:
            if item.get("kind") == "table":
                items.append({"kind": "table", "rows": [[clean(c) for c in row] for row in item.get("rows", [])]})
            else:
                text = clean(item.get("text", ""))
                if text:
                    items.append({"kind": "text", "text": text, "size": float(item.get("size") or 0),
                                  "bold": bool(item.get("bold"))})
        pages.append(items)
    wanted = [n for n, items in enumerate(pages)  # a table is text; a captioned drawing keeps its caption
              if not items or (all(i["kind"] == "text" for i in items)
                               and caption_only("\n".join(i["text"] for i in items)))]
    read = ocr_pages(data, wanted, memory, strict=True)
    for n, lines in read.items():
        lines = [dict(line, text=clean(line["text"]).strip("| ")) for line in lines]  # a ruling beside a line reads as "|"
        caption = {_squash(i["text"]) for i in pages[n]}
        pages[n] = pages[n] + [{"kind": "text", "text": line["text"], "size": float(line.get("size") or 0),
                                "bold": False, "ocr": True, "confidence": line.get("confidence", 100)}
                               for line in lines if line["text"] and _squash(line["text"]) not in caption]
    return pages


def _layout_reader():
    """What the kept layout depends on: the reader's code and the PDF libraries it uses. Any change reads PDFs again."""
    from importlib.metadata import version
    code = hashlib.sha256(Path(__file__).with_name("pdfchild.py").read_bytes()).hexdigest()[:16]
    return f"{code}/pdfplumber {version('pdfplumber')}/pypdfium2 {version('pypdfium2')}"


def _ocr_reader():
    """What kept OCR results depend on: OCR_VERSION and ocrchild.py's code. Any change reads scanned pages again."""
    code = hashlib.sha256(Path(__file__).with_name("ocrchild.py").read_bytes()).hexdigest()[:16]
    return f"{OCR_VERSION}/{code}"


def _layout_path(data):
    folder = os.environ.get("DOCS_DIR")
    return Path(folder) / ".layout" / f"{hashlib.sha256(data).hexdigest()}.json" if folder else None


def _kept_layout(data, max_pages):
    """The kept answer for this PDF, or None (none kept, another reader or page limit, or the file is damaged)."""
    path = _layout_path(data)
    try:
        saved = json.loads(path.read_text()) if path else None
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or saved.get("reader") != _layout_reader() or saved.get("max_pages") != max_pages:
        return None
    return saved.get("raw")


def _keep_layout(data, max_pages, raw):
    path = _layout_path(data)
    if path is None:
        return
    _write_whole(path, {"reader": _layout_reader(), "max_pages": max_pages, "raw": raw})


def _write_whole(path, value):
    """A kept answer in the library, written to a side file and then renamed: a reader never sees half a file, and a
    failed write leaves the earlier one as it was. A library that cannot be written is only slower."""
    part = path.with_suffix(f".{os.getpid()}-{threading.get_ident()}.part")
    try:
        path.parent.mkdir(exist_ok=True)
        part.write_text(json.dumps(value))
        part.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)


def extract_pdf(data, max_pages=PDF_MAX_PAGES, timeout=PDF_TIMEOUT_SECONDS, memory=PDF_MEMORY_BYTES):
    """Parsed in a child process with a time and memory limit; over-long documents are cut. Pages with no text layer
    (empty, or text drawn as shapes) are read with OCR when Tesseract is there (ocr_pages)."""
    raw = _run_child(data, [str(max_pages)], timeout, memory)
    blank = drawn_pages(raw.get("rows"))
    pages = ["" if n in blank else clean(p) for n, p in enumerate(raw["pages"][:max_pages])]
    read = ocr_pages(data, [n for n, text in enumerate(pages) if not text or caption_only(text)], memory)
    for n, lines in read.items():
        caption = {_squash(line) for line in pages[n].splitlines()}
        pages[n] = clean("\n".join([pages[n]] + [line["text"] for line in lines if _squash(line["text"]) not in caption]))
    return Extracted(pages=pages, title=None, drawn_text=bool(blank), ocr_pages=set(read))


def ocr_pages(data, indexes, memory=PDF_MEMORY_BYTES, strict=False):
    """{page index: [line, ...]} for the pages OCR could read in English (user, 2026-10-28: English only; a page that
    reads as Spanish or French is left out). Nothing when Tesseract is not installed or OCR fails. Results are kept in
    the library (DOCS_DIR/.ocr/<sha>.json), so `reread` and `sections` read a page once. When the reader fails on a
    set of pages, each is read alone; a page that fails alone is kept as empty for this reader (_ocr_reader)."""
    import shutil
    from crawler.sections import _language
    if not indexes or shutil.which("tesseract") is None:
        return {}
    cache, path = {}, None
    folder = os.environ.get("DOCS_DIR")
    if folder:
        path = Path(folder) / ".ocr" / f"{hashlib.sha256(data).hexdigest()}.json"
        try:
            saved = json.loads(path.read_text())
            if saved.get("version") == _ocr_reader():
                cache = saved.get("pages", {})
        except (OSError, ValueError):
            pass
    missing = [n for n in indexes if str(n) not in cache]
    if missing:
        try:
            cache.update(_ocr_child(data, missing, memory))
        except ExtractError:
            read = {}
            if len(missing) > 1:  # one page the reader cannot finish must not cost the others: read each alone
                for n in missing:
                    with contextlib.suppress(ExtractError):
                        read.update(_ocr_child(data, [n], memory))
            if not read:  # OCR itself may be broken: remember nothing, so the next run tries again
                if strict:  # sections: a build without its scanned pages must not replace a good one (kb._build_one)
                    raise
                return {}  # crawl and reread: the page stays without text and a later reread tries again
            cache.update(read)  # a page that failed alone is kept below as read with no text, not tried every run
        for n in missing:
            cache.setdefault(str(n), [])
        if path is not None:
            _write_whole(path, {"version": _ocr_reader(), "pages": cache})
    out = {}
    for n in indexes:
        lines = [line for line in cache.get(str(n), []) if clean(line.get("text", ""))]
        if lines and _language(" ".join(line["text"] for line in lines)) not in ("es", "fr"):
            out[n] = lines
    return out


def _ocr_child(data, indexes, memory):
    timeout = OCR_SECONDS_BASE + OCR_SECONDS_PER_PAGE * len(indexes)
    return _run_child(data, [",".join(str(n) for n in indexes)], timeout, memory, child="ocrchild.py")["pages"]


def drawn_pages(rows):
    """Indexes of the pages whose text layer is fragments: when the document has drawn text (drawn_text), each page with
    any drawn line. A Ruud IOM's English pages are real text while its Spanish and French pages are outlines; the
    warranty that started this has drawn lines on every page."""
    if not drawn_text(rows):
        return set()
    return {n for n, row in enumerate(rows or []) if int(row[0] or 0) > 0}


def drawn_text(rows):
    """True when enough of the PDF's text lines are drawn as shapes (pdfchild.drawn_rows) that its text is fragments.
    Decided for the whole document: a page of a few drawn labels on a diagram is normal."""
    try:
        drawn = sum(int(d) for d, _ in rows or [])
        real = sum(int(r) for _, r in rows or [])
    except (TypeError, ValueError):
        return False
    return drawn >= DRAWN_TEXT_MIN_LINES and drawn >= DRAWN_TEXT_SHARE * (drawn + real)


_running = set()  # reader processes now running, so an interrupted run can end them (stop_children)
_running_lock = threading.Lock()


def stop_children():
    """End every running reader at once: each runs in its own session, so Ctrl-C does not reach it."""
    with _running_lock:
        running = list(_running)
    for proc in running:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _run_child(data, args, timeout, memory, child="pdfchild.py"):
    """The child sets its own memory limit (CHILD_MEMORY_BYTES) first thing: preexec_fn is not safe once `sections`
    starts readers from threads."""
    child = Path(__file__).with_name(child)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",  # none of the container's secrets
           "OMP_THREAD_LIMIT": "1",  # Tesseract: one thread keeps its memory inside the child's limit
           "CHILD_MEMORY_BYTES": str(memory)}
    try:
        proc = subprocess.Popen([sys.executable, "-I", str(child), *args], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError as exc:
        raise ExtractError(f"could not start the PDF reader: {exc}") from exc
    with _running_lock:
        _running.add(proc)
    try:
        out, _ = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise ExtractError("the PDF took too long to read") from exc
    finally:
        with _running_lock:
            _running.discard(proc)
    if proc.returncode != 0:
        raise ExtractError("could not read the PDF")
    if len(out) > PDF_MAX_OUTPUT_BYTES:
        raise ExtractError("the PDF has too much text")
    try:
        answer = json.loads(out)
        answer["pages"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ExtractError("the PDF reader gave an unusable answer") from exc
    return answer


_WORD = re.compile(r"[A-Za-z]{2,}")


_NOT_A_CAPTION = re.compile(r"(notes?|memo|page|espa[ñn]ol|fran[çc]ais|english|intentionally ?left ?(blank|empty))\b.*",
                            re.I)


def caption_only(text):
    """A page whose text layer is only a caption over a drawing ("9. WIRING DIAGRAM", "3. UNIT DIMENSION"): under
    three real words, one of them 4+ letters, and not a Notes or blank page, a page number or a language header, nor
    Arabic (user, 2026-10-28: OCR those, not the others)."""
    if not text or has_text(text) or re.search(r"[\u0600-\u06FF]", text):
        return False
    words = " ".join(re.findall(r"[^\W\d_]+", text))
    return bool(re.search(r"[^\W\d_]{4,}", words)) and not _NOT_A_CAPTION.match(words)


def _squash(text):
    return re.sub(r"\W+", "", text).lower()


def has_text(text):
    """False for a page with no real text layer (a scan): fewer than three words of two or more letters."""
    return len(_WORD.findall(text)) >= 3


def index_page(conn, page_id, body, *, is_pdf, found=None):
    """Store the page's text (one row per page of the document) and decide: queue it for AI, or skip it.
    found: the text already read from this body (the same PDF at several addresses is read once)."""
    from crawler import extract

    if found is None:
        try:
            found = extract_pdf(body) if is_pdf else extract_html(body)
        except ExtractError:
            _exec(conn, "UPDATE pages SET extraction_status = 'failed' WHERE id = %s", (page_id,))
            return
    texts = found.pages
    for number, text in enumerate(texts, start=1):
        _exec(conn,
              "INSERT INTO page_text (page_id, page_number, title, text_sha, body, has_text) VALUES (%s, %s, %s, %s, %s, %s) "
              "ON CONFLICT (page_id, page_number) DO UPDATE SET title = EXCLUDED.title, text_sha = EXCLUDED.text_sha, "
              "body = EXCLUDED.body, has_text = EXCLUDED.has_text",
              (page_id, number, (found.title or "")[:500] or None, hashlib.sha256(text.encode("utf-8")).hexdigest(), text,
               has_text(text)))
    chosen = extract.pick_pages(texts) if is_pdf else []  # only PDFs are read by the AI
    if not chosen:
        status = "skipped"
    else:
        row = _rows(conn, "SELECT extraction_status, ai_extracted_sha, ai_extractor_version, ai_pending_sha FROM pages "
                          "WHERE id = %s", (page_id,))[0]
        sha = extract.doc_sha(texts, chosen)
        current = row[2] == extract.EXTRACTOR_VERSION
        if row[0] == "extracted" and row[1] == sha and current:
            status = "extracted"
        elif row[0] == "failed" and row[3] == sha and current:
            status = "failed"  # it failed on exactly this text already; only a change of text reopens it
        else:
            status = "waiting"
    _exec(conn, "UPDATE pages SET page_count = %s, extraction_status = %s"
                + ("" if status == "failed" else ", ai_retry_at = NULL") + " WHERE id = %s",
          (len(texts), status, page_id))


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
