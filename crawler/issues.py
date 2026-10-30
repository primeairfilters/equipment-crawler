"""Documents the code could not handle, kept until a code change fixes them (user, 2026-10-29).

One crawl.parse_issues row per file (content_sha) and kind, stamped with the version of the code part that failed:
- reader (textextract.reader_version: pdfchild.py, ocrchild.py, textextract.py and the PDF libraries):
  unreadable, cut_at_page_limit (recorded where the PDF is read), mostly_scanned and garbled_text (found by scan);
- tagger (extract.EXTRACTOR_VERSION): tagging_failed (recorded where the AI's answer is stored);
- sectioner (kb.build_version): no_sections (a live manual with no live sections, found by scan).

Each night (`auto`): retry, then scan, then the Markdown report.
- retry: an open problem whose part has a new version is taken again: the PDF read again (documents.reread, or an
  upload queued again), the document sent for tagging again, or left to the rebuild a new build version starts. One
  that is not seen again is 'fixed' with the version that fixed it; one seen again stays open under the new version.
- scan: the stored-state checks, and the document link for problems recorded before their document existed.
- report: every open problem as one Markdown fix list (control.issue_report), the same as `./crawl issues`.
A 'wont_fix' problem (set on the admin page) is never retried. Nothing here deletes; callers commit.
"""
import json
import re
from datetime import datetime, timezone

KINDS = {  # kind: (component, what it means, where a fix usually goes)
    "unreadable": ("reader", "The PDF reader failed on the file: no text at all.",
                   "crawler/pdfchild.py (the reader), crawler/textextract.py (limits, run_child)"),
    "cut_at_page_limit": ("reader", "The PDF has more pages than PDF_MAX_PAGES, so only the first part was read.",
                          "crawler/textextract.py PDF_MAX_PAGES (memory and time limits with it)"),
    "mostly_scanned": ("reader", "Most pages have no text even after OCR.",
                       "crawler/ocrchild.py and textextract.ocr_pages (OCR), drawn_pages"),
    "garbled_text": ("reader", "Pages were read but the text is not words: letters with no vowels (a font with no "
                     "text map, a bad encoding) or words run together with no spaces.",
                     "crawler/pdfchild.py page_text, textextract.clean"),
    "tagging_failed": ("tagger", "The AI's tags could not be used or stored.",
                       "crawler/extract.py (prompt, schema, check_tags), crawler/store.save_document"),
    "no_sections": ("sectioner", "A live manual has no live sections, so Breeze cannot use it.",
                    "crawler/sections.py (headings, cutting), crawler/kb.py build"),
}
GARBLED_PAGE = 0.5  # a page whose words are fewer than this share of its tokens is garbled
GARBLED_MIN_TOKENS = 30
GARBLED_DOCUMENT = 0.2  # a document with at least this share of garbled text pages,
GARBLED_PAGES = 3  # or at least this many
SCANNED_SHARE = 0.5  # text on fewer than this share of pages, after OCR
SAMPLE_CHARS = 300
# OCR reads English only (user, 2026-10-28): pages of another language left without text are not a code problem
# text on too few pages, not counting the pages the reader left out as Spanish or French (docs 69 and 70)
_SCANNED = ("d.page_count > 0 AND d.state <> 'retired' AND COALESCE(d.language, 'en') = 'en' AND d.text_pages + "
            "(SELECT COUNT(*) FROM page_text t WHERE t.page_id = (SELECT MIN(page_id) FROM document_urls "
            "WHERE document_id = d.id) AND NOT t.has_text AND t.left_out = 'language') < d.page_count * %s")
GLUED = 20  # letters in one run: longer is words run together (the longest Ruud word seen is shorter)
ABBREVIATION = 5
_VOWEL = re.compile(r"[aeiouyàâäáéèêëíîïóôöúùûüÿ]", re.I)
_RUN = re.compile(r"[^\W\d_]+")  # a run of letters, any script
_LATIN = re.compile(r"[A-Za-zÀ-ÿ]")


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def version(component):
    if component == "reader":
        from crawler.textextract import reader_version
        return reader_version()
    if component == "tagger":
        from crawler.extract import EXTRACTOR_VERSION
        return EXTRACTOR_VERSION
    from crawler.kb import build_version
    return build_version()


def record(conn, content_sha, kind, detail, document_id=None):
    """Note a problem (or see it again). A fixed problem seen again is open again."""
    component = KINDS[kind][0]
    current = version(component)
    _rows(conn, "INSERT INTO parse_issues (content_sha, document_id, kind, component, detail, first_version, seen_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (content_sha, kind) DO UPDATE SET "
                "detail = EXCLUDED.detail, seen_version = EXCLUDED.seen_version, times_seen = parse_issues.times_seen + 1, "
                "last_seen_at = (now() AT TIME ZONE 'UTC'), document_id = COALESCE(EXCLUDED.document_id, "
                "parse_issues.document_id), state = CASE WHEN parse_issues.state = 'wont_fix' THEN 'wont_fix' "
                "ELSE 'open' END, fixed_version = NULL, fixed_at = NULL",
          (content_sha, document_id, kind, component, json.dumps(detail, default=str), current, current))


def garbled_pages(texts):
    """[(page number, share of real words, sample)] of the pages whose text is not words: runs of letters with no
    vowel (a font with no text map), or words run together (a missing space: "Pointsoutanimmediatehazardous").
    Numbers, codes and short capital abbreviations (CFM, WB) say nothing; any non-Latin script (Arabic) is words."""
    out = []
    for number, text in enumerate(texts, start=1):
        good = bad = 0
        for run in _RUN.findall(text):
            if not run.isascii() and not _LATIN.search(run):
                good += 1  # another script: its words are words
            elif len(run) > GLUED:
                bad += 1
            elif (run.isupper() and len(run) <= ABBREVIATION) or len(run) <= 3:
                continue  # CFM, WB; mm, ft, lbs
            elif _VOWEL.search(run) or len(run) == 1:
                good += 1
            else:
                bad += 1
        if good + bad >= GARBLED_MIN_TOKENS and good / (good + bad) < GARBLED_PAGE:
            out.append((number, round(good / (good + bad), 2), " ".join(text.split())[:SAMPLE_CHARS]))
    return out


def scan(conn):
    """The stored-state checks. Returns {kind: problems found this scan}."""
    found = {"mostly_scanned": 0, "garbled_text": 0, "no_sections": 0}
    _link(conn)
    reader = version("reader")
    for document_id, sha, pages, text_pages in _rows(
            conn, "SELECT id, content_sha, page_count, text_pages FROM documents d WHERE " + _SCANNED, (SCANNED_SHARE,)):
        record(conn, sha, "mostly_scanned", {"pages": pages, "pages_with_text": text_pages}, document_id)
        found["mostly_scanned"] += 1
    _settle(conn, "mostly_scanned", "SELECT content_sha FROM documents d WHERE " + _SCANNED, (SCANNED_SHARE,))
    for document_id, sha in _rows(
            conn, "SELECT d.id, d.content_sha FROM documents d WHERE d.page_count > 0 AND d.state <> 'retired' AND NOT "
                  "EXISTS (SELECT 1 FROM issue_checks c WHERE c.document_id = d.id AND c.check_name = 'garbled_text' "
                  "AND c.version = %s) ORDER BY d.id", (reader,)):
        texts = [r[0] or "" for r in _rows(
            conn, "SELECT t.body FROM page_text t WHERE t.page_id = (SELECT MIN(page_id) FROM document_urls "
                  "WHERE document_id = %s) ORDER BY t.page_number", (document_id,))]
        bad = garbled_pages(texts)
        with_text = sum(1 for t in texts if t.strip())
        if bad and len(bad) >= min(GARBLED_PAGES, max(1, GARBLED_DOCUMENT * with_text)):
            record(conn, sha, "garbled_text", {"pages_with_text": with_text, "garbled_pages": [b[0] for b in bad],
                                               "samples": [{"page": n, "words": share, "text": sample}
                                                           for n, share, sample in bad[:3]]}, document_id)
            found["garbled_text"] += 1
        else:
            _fixed(conn, sha, "garbled_text", reader)
        _rows(conn, "INSERT INTO issue_checks (document_id, check_name, version) VALUES (%s, 'garbled_text', %s) "
                    "ON CONFLICT (document_id, check_name) DO UPDATE SET version = EXCLUDED.version, "
                    "checked_at = (now() AT TIME ZONE 'UTC')", (document_id, reader))
    live_without = ("SELECT d.content_sha FROM documents d WHERE d.state = 'live' AND d.hidden_at IS NULL "
                    "AND d.doc_type IS DISTINCT FROM 'not_equipment' AND NOT EXISTS "
                    "(SELECT 1 FROM sections s WHERE s.document_id = d.id AND s.is_live)")
    for (sha,) in _rows(conn, live_without):
        document_id = _rows(conn, "SELECT id, page_count FROM documents WHERE content_sha = %s", (sha,))[0]
        record(conn, sha, "no_sections", {"pages": document_id[1]}, document_id[0])
        found["no_sections"] += 1
    _settle(conn, "no_sections", live_without)
    return found


def _link(conn):
    """Problems recorded while the file was read, before its document existed, get their document."""
    _rows(conn, "UPDATE parse_issues i SET document_id = d.id FROM documents d WHERE i.document_id IS NULL "
                "AND d.content_sha = i.content_sha")


def _settle(conn, kind, still_sql, args=()):
    """Open problems of a scan-found kind that the scan no longer finds are fixed."""
    still = {r[0] for r in _rows(conn, still_sql, args)}
    current = version(KINDS[kind][0])
    for (sha,) in _rows(conn, "SELECT content_sha FROM parse_issues WHERE kind = %s AND state IN ('open', 'retrying')",
                        (kind,)):
        if sha not in still:
            _fixed(conn, sha, kind, current)


def _fixed(conn, sha, kind, current):
    _rows(conn, "UPDATE parse_issues SET state = 'fixed', fixed_version = %s, fixed_at = (now() AT TIME ZONE 'UTC') "
                "WHERE content_sha = %s AND kind = %s AND state IN ('open', 'retrying')", (current, sha, kind))


def retry(conn, after_document=None):
    """Take again every open problem whose code part changed since it was seen. Returns {"retried", "fixed"}."""
    from crawler import documents
    out = {"retried": 0, "fixed": 0}
    _link(conn)
    reader, tagger = version("reader"), version("tagger")
    # a problem with nothing to take again (no document and no upload yet) waits until it has one
    stale = _rows(conn, "SELECT id, content_sha, kind, component, document_id, detail FROM parse_issues "
                        "WHERE state = 'open' AND seen_version <> CASE component WHEN 'reader' THEN %s "
                        "WHEN 'tagger' THEN %s ELSE seen_version END AND (document_id IS NOT NULL "
                        "OR detail ? 'upload_id' OR detail ? 'page_id') ORDER BY id", (reader, tagger))
    reread = sorted({r[4] for r in stale if r[3] == "reader" and r[4] is not None})
    read = []
    if reread:  # only documents whose original was really read again can be judged (a missing original is not)
        read = documents.reread(conn, documents=reread, after_document=after_document)["documents"]
    for issue_id, sha, kind, component, document_id, detail in stale:
        out["retried"] += 1
        if component == "tagger":
            _rows(conn, "UPDATE pages SET extraction_status = 'waiting', ai_retry_at = NULL WHERE extraction_status = "
                        "'failed' AND (id = %s OR id IN (SELECT page_id FROM document_urls WHERE document_id = %s))",
                  ((detail or {}).get("page_id"), document_id))
            _rows(conn, "UPDATE parse_issues SET state = 'retrying' WHERE id = %s", (issue_id,))
            continue
        if document_id is None and (detail or {}).get("upload_id"):
            _rows(conn, "UPDATE control.uploads SET state = 'queued', reason = NULL WHERE id = %s AND state = 'rejected'",
                  (detail["upload_id"],))
            _rows(conn, "UPDATE parse_issues SET state = 'retrying' WHERE id = %s", (issue_id,))
            continue
    if reread:
        scan(conn)  # the scan-found kinds are judged again on the new text
    # a reader problem not seen again under the new reader is fixed (the read records it again when it is not)
    for issue_id, kind in _rows(conn, "SELECT id, kind FROM parse_issues WHERE state = 'open' AND component = 'reader' "
                                      "AND seen_version <> %s AND document_id = ANY (%s)", (reader, read or [0])):
        _rows(conn, "UPDATE parse_issues SET state = 'fixed', fixed_version = %s, fixed_at = (now() AT TIME ZONE 'UTC') "
                    "WHERE id = %s", (reader, issue_id))
        out["fixed"] += 1
    # tagging retried: fixed once the document's page is tagged under the current version
    out["fixed"] += len(_rows(
        conn, "UPDATE parse_issues i SET state = 'fixed', fixed_version = %s, fixed_at = (now() AT TIME ZONE 'UTC') "
              "WHERE i.state = 'retrying' AND i.component = 'tagger' AND EXISTS (SELECT 1 FROM pages p "
              "WHERE (p.id = (i.detail ->> 'page_id')::bigint OR p.id IN (SELECT page_id FROM document_urls "
              "WHERE document_id = i.document_id)) AND p.extraction_status = 'extracted' "
              "AND p.ai_extractor_version = %s) RETURNING i.id", (tagger, tagger)))
    return out


def report(conn, now=None):
    """Every open problem as one Markdown fix list for Claude, grouped by kind. Stored as control.issue_report
    'latest' and returned."""
    now = now or _now()
    _link(conn)
    rows = _rows(conn, "SELECT i.kind, i.id, i.content_sha, i.document_id, d.title, m.name, d.page_count, d.canonical_url, "
                       "i.detail, i.times_seen, i.first_seen_at, i.seen_version FROM parse_issues i "
                       "LEFT JOIN documents d ON d.id = i.document_id LEFT JOIN manufacturers m ON m.id = d.manufacturer_id "
                       "WHERE i.state = 'open' ORDER BY i.kind, i.id")
    versions = {c: version(c) for c in ("reader", "tagger", "sectioner")}
    lines = [f"# Crawler parse issues ({now:%Y-%m-%d %H:%M} UTC)", "",
             f"{len(rows)} open problem(s) the crawler's code could not handle. Fix the code (repo equipment-crawler), "
             "test-first, so each kind below is handled; the version of the part you change moves with the change "
             "(the reader's from its files, the tagger's EXTRACTOR_VERSION, the sectioner's SECTIONER_VERSION), and the "
             "next nightly run takes these documents again and marks each fixed or still open.", "",
             f"Versions now: reader `{versions['reader']}`, tagger `{versions['tagger']}`, sectioner "
             f"`{versions['sectioner']}`. Originals are in the docs bucket (or DOCS_DIR) as `<sha256>.pdf`.", ""]
    if not rows:
        lines.append("Nothing open.")
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row[0], []).append(row)
    for kind, items in by_kind.items():
        component, meaning, where = KINDS[kind]
        lines += [f"## {kind} ({len(items)})", "", meaning, "", f"Part: {component}. Usually fixed in: {where}.", ""]
        for (_, issue_id, sha, document_id, title, brand, pages, url, detail, seen, first, seen_version) in items:
            lines.append(f"### Issue {issue_id}: document {document_id if document_id is not None else '(none yet)'}"
                         + (f", {title}" if title else ""))
            lines.append("")
            lines.append(f"- Original: `{sha}.pdf`" + (f" (found at {url})" if url else ""))
            if brand or pages is not None:
                lines.append(f"- Brand: {brand or 'none'}; pages read: {pages if pages is not None else 'none'}")
            lines.append(f"- Seen {seen} time(s) since {first:%Y-%m-%d}, last under version `{seen_version}`")
            for key, value in (detail or {}).items():
                if key == "samples":
                    for sample in value:
                        lines.append(f"- Sample, page {sample.get('page')} ({sample.get('words')} of tokens are "
                                     f"words): `{sample.get('text', '')}`")
                else:
                    lines.append(f"- {key.replace('_', ' ')}: {value}")
            lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    _rows(conn, "INSERT INTO control.issue_report (name, made_at, open_issues, markdown) VALUES ('latest', %s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET made_at = EXCLUDED.made_at, open_issues = EXCLUDED.open_issues, "
                "markdown = EXCLUDED.markdown", (now, len(rows), text))
    return text
