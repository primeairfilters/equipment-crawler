"""Text out of HTML and PDF, stored in page_text, with the free filter deciding what AI reads."""
import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

PDF_MAX_PAGES = 200
PDF_TIMEOUT_SECONDS = 60
PDF_MEMORY_BYTES = 1024 ** 3
PDF_MAX_OUTPUT_BYTES = 20 * 1024 * 1024

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


def clean(text):
    """Ligatures fixed, runs of spaces collapsed per line, blank lines dropped. Same words give the same text."""
    text = text.translate(_LIGATURES).replace("\x00", "")
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


def extract_pdf(data, max_pages=PDF_MAX_PAGES, timeout=PDF_TIMEOUT_SECONDS, memory=PDF_MEMORY_BYTES):
    """Parsed in a child process with a time and memory limit; over-long documents are cut."""
    def limit():
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

    child = Path(__file__).with_name("pdfchild.py")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}  # none of the container's secrets
    try:
        proc = subprocess.Popen([sys.executable, "-I", str(child), str(max_pages)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, preexec_fn=limit,
                                start_new_session=True)
    except OSError as exc:
        raise ExtractError(f"could not start the PDF reader: {exc}") from exc
    try:
        out, _ = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise ExtractError("the PDF took too long to read") from exc
    if proc.returncode != 0:
        raise ExtractError("could not read the PDF")
    if len(out) > PDF_MAX_OUTPUT_BYTES:
        raise ExtractError("the PDF has too much text")
    done = type("Done", (), {"stdout": out})
    try:
        pages = json.loads(done.stdout)["pages"]
    except (ValueError, KeyError) as exc:
        raise ExtractError("the PDF reader gave an unusable answer") from exc
    return Extracted(pages=[clean(p) for p in pages[:max_pages]], title=None)


def index_page(conn, page_id, body, *, is_pdf):
    """Store the page's text (one row per page of the document) and decide: queue it for AI, or skip it."""
    from crawler import extract

    try:
        found = extract_pdf(body) if is_pdf else extract_html(body)
    except ExtractError:
        _exec(conn, "UPDATE pages SET extraction_status = 'failed' WHERE id = %s", (page_id,))
        return
    texts = found.pages
    for number, text in enumerate(texts, start=1):
        _exec(conn,
              "INSERT INTO page_text (page_id, page_number, title, text_sha, body) VALUES (%s, %s, %s, %s, %s) "
              "ON DUPLICATE KEY UPDATE title = VALUES(title), text_sha = VALUES(text_sha), body = VALUES(body)",
              (page_id, number, (found.title or "")[:500] or None, hashlib.sha256(text.encode("utf-8")).hexdigest(), text))
    chosen = extract.select_pages(texts)
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
