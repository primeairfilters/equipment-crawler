"""A fetched PDF becomes a document the moment it is stored: one per unique content (D40), with every address it
was found at (document_urls) and how many of its pages carry text (D59). Tagging and sectioning come later.

A PDF whose address used to serve other content is a new version: the new document records the one it replaces
(D55). Nothing here commits, deletes or changes an older document.
"""
import hashlib

from crawler import library
from crawler.store import page_manufacturer


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def record_pdf(conn, brand_id, page_id, url, body):
    """The document id for this PDF, made if new. Call after the page's text is stored."""
    sha = hashlib.sha256(body).hexdigest()
    pages, text_pages = _rows(conn, "SELECT COUNT(*), COUNT(*) FILTER (WHERE has_text) FROM page_text WHERE page_id = %s",
                              (page_id,))[0]
    earlier = _rows(conn, "SELECT du.document_id FROM document_urls du JOIN documents d ON d.id = du.document_id "
                          "WHERE du.page_id = %s AND d.content_sha <> %s ORDER BY du.last_seen_at DESC, du.document_id DESC "
                          "LIMIT 1", (page_id, sha))
    maker = page_manufacturer(conn, brand_id, page_id)
    found = _rows(conn, "SELECT id FROM documents WHERE content_sha = %s", (sha,))
    if found:
        document_id = found[0][0]
    else:
        document_id = _rows(
            conn,
            "INSERT INTO documents (content_sha, manufacturer_id, canonical_url, page_count, text_pages, needs_ocr, replaces_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (content_sha) DO UPDATE SET content_sha = EXCLUDED.content_sha "
            "RETURNING id",
            (sha, maker, url, pages, text_pages, text_pages < pages,
             earlier[0][0] if earlier else None))[0][0]
        if earlier:  # a new version of a hidden manual stays hidden until the user says otherwise (D56)
            hidden = _rows(conn, "SELECT hidden_reason FROM documents WHERE id = %s AND hidden_at IS NOT NULL",
                           (earlier[0][0],))
            if hidden:
                _rows(conn, "UPDATE documents SET hidden_at = (now() AT TIME ZONE 'UTC'), hidden_reason = %s WHERE id = %s",
                      (hidden[0][0], document_id))
                _rows(conn, "INSERT INTO visibility_log (target_type, target_id, hidden, reason, by_whom) "
                            "VALUES ('document', %s, true, %s, 'crawler: new version of a hidden manual')",
                      (document_id, hidden[0][0]))
    _rows(conn, "INSERT INTO document_urls (document_id, page_id, url) VALUES (%s, %s, %s) "
                "ON CONFLICT (document_id, page_id) DO UPDATE SET last_seen_at = (now() AT TIME ZONE 'UTC'), url = EXCLUDED.url",
          (document_id, page_id, url))
    # every brand that serves this file owns it too (Q23): a Rheem address serving a Ruud manual makes it Rheem's
    if maker is not None:
        _rows(conn, "INSERT INTO document_brands (document_id, manufacturer_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
              (document_id, maker))
    return document_id


def reread(conn, brand_id=None, after_document=None, documents=None):
    """Read again every kept PDF whose document has no pages (the reader failed on it, e.g. a big manual before the
    reader closed pages as it went) or needs OCR (a better reader may recover pages; a true scan comes out the same).
    documents: read exactly these instead (a manual cut at an older page limit: nothing stored says it was cut).
    Fetches nothing. Each address that served it gets the page text; the document gets
    its page count and OCR flag. Returns {"read": n, "unreadable": n}."""
    from crawler.textextract import ExtractError, extract_pdf, index_page

    if not library.configured():
        return {"read": 0, "unreadable": 0, "documents": []}
    docs = _rows(conn, "SELECT DISTINCT d.id, d.content_sha FROM documents d JOIN document_urls du ON du.document_id = d.id "
                       "JOIN pages p ON p.id = du.page_id WHERE (CASE WHEN %s::bigint[] IS NULL "
                       "THEN d.page_count = 0 OR d.needs_ocr ELSE d.id = ANY (%s::bigint[]) END) "
                       "AND (%s::bigint IS NULL OR p.brand_id = %s) ORDER BY d.id",
                 (documents, documents, brand_id, brand_id))
    read = unreadable = 0
    done = []  # the documents whose original was read again (parse issues: only these can be fixed)
    for document_id, sha in docs:
        body = library.read(sha)
        if body is None:
            unreadable += 1
            continue
        try:
            found = extract_pdf(body)
        except ExtractError as exc:
            from crawler import issues
            issues.record(conn, sha, "unreadable", {"error": str(exc), "reader said": exc.detail, "bytes": len(body)},
                          document_id)
            unreadable += 1
            continue
        page_ids = [r[0] for r in _rows(conn, "SELECT page_id FROM document_urls WHERE document_id = %s ORDER BY page_id",
                                        (document_id,))]
        done.append(document_id)
        for page_id in page_ids:
            index_page(conn, page_id, body, is_pdf=True, found=found)
        pages, text_pages = _rows(conn, "SELECT COUNT(*), COUNT(*) FILTER (WHERE has_text) FROM page_text WHERE page_id = %s",
                                  (page_ids[0],))[0]
        _rows(conn, "UPDATE documents SET page_count = %s, text_pages = %s, needs_ocr = %s, "
                    "updated_at = (now() AT TIME ZONE 'UTC') WHERE id = %s", (pages, text_pages, text_pages < pages, document_id))
        if pages:
            read += 1
        else:
            unreadable += 1
        if after_document:
            after_document()
    return {"read": read, "unreadable": unreadable, "documents": done}
