"""P19 (D84): documents uploaded by hand on the admin page. Each queued control.uploads row:

1. Its file is read from inbox/ (the docs bucket, or DOCS_DIR/inbox without one). Missing, over MAX_PDF_BYTES, not a
   file it reads, a price sheet, unreadable, or with nothing to read: 'rejected' with the reason. It reads PDFs (by their
   content), and spreadsheets, Word, PowerPoint, saved web pages and photos (by their name; EQUIPMENT_FILE_UPLOADS.md,
   crawler/filereaders.py).
2. A file already stored (same sha256): 'already_had' with its document; a brand picked on the page is added to it.
3. Otherwise the original is stored once under its sha256 like any crawled PDF, and a page row carries it through
   text, tagging and sections: under the brand picked on the page; else the one brand its file name names (user
   2026-11-01: "Fujitsu AirStage H-Series – Document Links.xlsx"); else the brand its text names most (at least
   MIN_MENTIONS times and more than any other, user 2026-10-29); else no brand, under the stopped UPLOADS_BRAND, which
   is never crawled, and the AI may name it when it tags the file (extract.upload_brand). 'added' with its document.
   An upload is trusted: no host check.

The page row's address (upload://<id>/<file name>) is never fetched: it has no start address and no recheck date.
A row left 'processing' by a crash is taken again. Nothing here commits; after_upload runs after each one.
"""
import hashlib
import re
from urllib.parse import quote

from crawler import library
from crawler.documents import record_pdf
from crawler.fetch import MAX_PDF_BYTES
from crawler.filereaders import extract, file_type as _file_type, nothing_to_read
from crawler.sheets import SHEET_TYPES, price_sheet
from crawler.store import alias_key, brand_manufacturer
from crawler.textextract import ExtractError, extract_pdf, index_page

UPLOADS_BRAND = "(uploads)"  # the brand of an upload with none picked or named; stopped, so never crawled
UPLOADS_HOST = "uploads.invalid"
MIN_MENTIONS = 2  # a brand named once (a "replaces a Carrier unit") is not the document's brand
SHORT_NAME = 3  # a brand this short (LG, GE) counts only written in capitals


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def process_uploads(conn, after_upload=None):
    """Take every queued upload. Returns {"added", "already_had", "rejected"} counts."""
    summary = {"added": 0, "already_had": 0, "rejected": 0}
    for upload_id, key, name, crawl_brand in _rows(
            conn, "SELECT u.id, u.s3_key, u.file_name, c.crawl_brand_id FROM control.uploads u "
                  "LEFT JOIN control.brands c ON c.id = u.brand_id WHERE u.state IN ('queued', 'processing') "
                  "ORDER BY u.id"):
        _rows(conn, "UPDATE control.uploads SET state = 'processing' WHERE id = %s", (upload_id,))
        _rows(conn, "SAVEPOINT upload")
        try:
            state, reason, document_id, sha = _take(conn, upload_id, key, name, crawl_brand)
            _rows(conn, "RELEASE SAVEPOINT upload")
        except library.LibraryError:
            _rows(conn, "ROLLBACK TO SAVEPOINT upload")
            raise  # the store is down: every upload would fail the same way; the row stays and the next run retries
        except Exception as exc:  # this file breaks the code: rejected with the reason, the rest go on (a parse issue)
            _rows(conn, "ROLLBACK TO SAVEPOINT upload")
            state, reason, document_id, sha = "rejected", f"the crawler failed on it ({exc.__class__.__name__}: " \
                                                          f"{str(exc)[:200]})", None, None
            _crashed(conn, upload_id, key, name, exc)
        _settle_issue(conn, upload_id, sha, state, reason)
        _rows(conn, "UPDATE control.uploads SET state = %s, reason = %s, document_id = %s, "
                    "processed_at = (now() AT TIME ZONE 'UTC') WHERE id = %s", (state, reason, document_id, upload_id))
        summary[state] += 1
        if after_upload is not None:
            after_upload()
    return summary


def _take(conn, upload_id, key, name, crawl_brand):
    """(state, reason, document_id, sha256 or None) for one upload."""
    try:
        body = library.read_inbox(key)
    except ValueError:
        body = None
    if body is None:
        return "rejected", f"the file is not in the inbox ({key})", None, None
    if len(body) > MAX_PDF_BYTES:
        return "rejected", f"larger than {MAX_PDF_BYTES // (1024 * 1024)} MB", None, None
    file_type = _file_type(body, name)
    if file_type is None:
        return "rejected", "not a PDF" if name.lower().endswith(".pdf") else "not a file Tracforce reads", None, None
    if file_type in SHEET_TYPES and price_sheet(name):
        return "rejected", "a price sheet", None, None
    sha = hashlib.sha256(body).hexdigest()
    stored = _rows(conn, "SELECT id FROM documents WHERE content_sha = %s", (sha,))
    if stored:
        if crawl_brand is not None:  # the brand picked on the page is the document's too (Q23)
            _rows(conn, "INSERT INTO document_brands (document_id, manufacturer_id) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING", (stored[0][0], brand_manufacturer(conn, crawl_brand)))
        return "already_had", None, stored[0][0], sha
    try:
        found = extract_pdf(body) if file_type == "pdf" else extract(body, file_type)
    except ExtractError as exc:
        from crawler import issues
        issues.record(conn, sha, "unreadable", {"error": str(exc), "reader said": exc.detail, "bytes": len(body),
                                                "upload_id": upload_id, "file_name": name,
                                                "file_type": file_type})  # queued again after a fix
        what = "a PDF" if file_type == "pdf" else "a spreadsheet" if file_type in SHEET_TYPES else f"a .{file_type} file"
        return "rejected", f"could not be read as {what} ({str(exc)[:120]})", None, sha
    empty = None if file_type == "pdf" else nothing_to_read(found, file_type)
    if empty:
        return "rejected", empty, None, sha
    library.save(body, file_type)  # LibraryError: the row stays 'processing' and the next run takes it again
    if crawl_brand is not None:
        brand_id, maker = crawl_brand, brand_manufacturer(conn, crawl_brand)
    else:
        brand_id, maker = named_brand(conn, re.sub(r"[_.]+", " ", name.rsplit(".", 1)[0]), min_mentions=1)
        if brand_id is None:
            brand_id, maker = named_brand(conn, "\n".join(found.pages))
        brand_id = brand_id or _uploads_brand(conn)
    host_id = _rows(conn, "INSERT INTO hosts (host) VALUES (%s) ON CONFLICT (host) DO UPDATE SET host = EXCLUDED.host "
                          "RETURNING id", (UPLOADS_HOST,))[0][0]
    url = f"upload://{upload_id}/{quote(name)}"
    page_id = _rows(conn, "INSERT INTO pages (brand_id, host_id, url, url_sha, is_pdf, content_type, content_sha, "
                          "fetched_at, fetch_state, decision, decided_by, decision_reason) VALUES (%s, %s, %s, %s, %s, "
                          "%s, %s, (now() AT TIME ZONE 'UTC'), 'fetched', 'crawl', 'user', "
                          "'uploaded on the admin page') RETURNING id",
                    (brand_id, host_id, url, hashlib.sha256(url.encode("utf-8")).hexdigest(), file_type == "pdf",
                     library.TYPES[file_type], sha))[0][0]
    _rows(conn, "UPDATE control.uploads SET page_id = %s, manufacturer_id = %s WHERE id = %s",
          (page_id, maker, upload_id))  # before record_pdf, which reads the manufacturer from here
    # is_pdf here means "a document the AI tags": every upload is one, and found is given, so nothing is read again
    index_page(conn, page_id, body, is_pdf=True, found=found)
    return "added", None, record_pdf(conn, brand_id, page_id, url, body, file_type=file_type), sha



def _crashed(conn, upload_id, key, name, exc):
    """An upload that broke the code is a parse issue, so a fix queues it again (crawler/issues.py)."""
    from crawler import issues
    body = library.read_inbox(key) if key.startswith("inbox/") else None
    if body:
        issues.record(conn, hashlib.sha256(body).hexdigest(), "unreadable",
                      {"error": f"{exc.__class__.__name__}: {str(exc)[:500]}", "upload_id": upload_id,
                       "file_name": name, "bytes": len(body), "file_type": _file_type(body, name) or "pdf"})


def _settle_issue(conn, upload_id, sha, state, reason):
    """A requeued upload's parse issue: fixed once it is taken in; open again if its file is gone from the inbox."""
    if state in ("added", "already_had") and sha:
        from crawler.issues import version
        _rows(conn, "UPDATE parse_issues SET state = 'fixed', fixed_version = %s, fixed_at = (now() AT TIME ZONE 'UTC') "
                    "WHERE content_sha = %s AND kind = 'unreadable' AND state IN ('open', 'retrying')",
              (version("reader"), sha))
    elif state == "rejected" and reason and reason.startswith("the file is not in the inbox"):
        _rows(conn, "UPDATE parse_issues SET state = 'open', note = 'its upload is gone from the inbox: upload it again' "
                    "WHERE state = 'retrying' AND (detail ->> 'upload_id')::bigint = %s", (upload_id,))


def named_brand(conn, text, min_mentions=MIN_MENTIONS):
    """(crawler brand id or None, manufacturer id or None) of the brand the text names most: the crawler's brands
    and the manufacturers it knows, counted by whole-word mentions. min_mentions=1 for a short text that names a brand
    once (a file name, the AI's answer); a tie still names none."""
    counts, makers = {}, {}
    for brand_id, name in _rows(conn, "SELECT id, name FROM brands WHERE name_key <> %s", (UPLOADS_BRAND,)):
        maker = _rows(conn, "SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s", (alias_key(name),))
        group = ("maker", maker[0][0]) if maker else ("brand", brand_id)
        counts[group] = max(counts.get(group, 0), _mentions(name, text))
        makers.setdefault(group, brand_id)
    for maker_id, alias in _rows(conn, "SELECT manufacturer_id, alias FROM manufacturer_aliases"):
        group = ("maker", maker_id)
        counts[group] = max(counts.get(group, 0), _mentions(alias, text))
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] < min_mentions or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
        return None, None
    (kind, value), _ = ranked[0]
    brand_id = makers.get((kind, value))
    if kind == "brand":
        return brand_id, brand_manufacturer(conn, brand_id)
    return brand_id, value


def _mentions(name, text):
    name = " ".join(name.split())
    if not name:
        return 0
    flags = 0 if len(name) <= SHORT_NAME else re.IGNORECASE
    return len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", text, flags))


def _uploads_brand(conn):
    return _rows(conn, "INSERT INTO brands (name, name_key, status) VALUES (%s, %s, 'stopped') ON CONFLICT (name_key) "
                       "DO UPDATE SET name_key = EXCLUDED.name_key RETURNING id", (UPLOADS_BRAND, UPLOADS_BRAND))[0][0]


def uploads_brand_id(conn):
    """The UPLOADS_BRAND's id, or None before the first upload without a brand."""
    found = _rows(conn, "SELECT id FROM brands WHERE name_key = %s", (UPLOADS_BRAND,))
    return found[0][0] if found else None
