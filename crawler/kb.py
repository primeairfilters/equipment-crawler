"""The serving layer's lifecycle: build sections, publish one build live, hide and unhide, and check (D48, D51, D53,
D56).

- build: cut a tagged document into sections (crawler/sections.py) as a new build next to earlier ones, with its
  filters copied onto every row and its codes in section_terms. Nothing goes live. The same text makes no new build.
- publish: in one step, one build's sections go live and every other build of that document goes dark; the version
  it replaces is retired. The gate refuses a hidden document, a hidden or switched-off manufacturer, an untagged
  document and anything tagged not_equipment, so a later crawl can never bring hidden content back.
- hide / unhide: the only way to switch visibility. Each sets the flag, flips is_live and writes visibility_log in
  one step. unhide puts back only the live build.
- check: a read-only health report.

Nothing here deletes a row. Callers commit.
"""
import os

from crawler import library
from crawler.brands import UnknownTarget
from crawler.sections import SECTIONER_VERSION, build_sections, terms
from crawler.store import alias_key
from crawler.textextract import LAYOUT_TIMEOUT_SECONDS, ExtractError, extract_layout

LAYOUT_SECONDS_PER_PAGE = 5

FILTERS = ("manufacturer_id", "trade", "equipment_type", "doc_type", "language")


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount


# ---------------------------------------------------------------- build

def document_pages(conn, document_id, problems=None):
    """Layout items per page: from the original PDF (with font sizes) when the library has it, else from the stored
    page text, one line per text line and no sizes (which makes the sectioner fall back to windows). When the original
    could not be read, (document id, why) goes into problems: a fallback loses headings and tables, so it must show.
    The time limit grows with the page count (a 69-page capacity chart takes about 140 s)."""
    sha, page_count = _rows(conn, "SELECT content_sha, page_count FROM documents WHERE id = %s", (document_id,))[0]
    folder = os.environ.get("DOCS_DIR")
    if library.configured() and library.has_file(sha):
        with open(os.path.join(folder, f"{sha}.pdf"), "rb") as handle:
            try:
                return extract_layout(handle.read(),
                                      timeout=max(LAYOUT_TIMEOUT_SECONDS, LAYOUT_SECONDS_PER_PAGE * (page_count or 0)))
            except ExtractError as exc:
                if problems is not None:
                    problems.append((document_id, str(exc)))
    page = _rows(conn, "SELECT du.page_id FROM document_urls du WHERE du.document_id = %s ORDER BY du.last_seen_at DESC "
                       "LIMIT 1", (document_id,))
    if not page:
        return []
    texts = _rows(conn, "SELECT body FROM page_text WHERE page_id = %s ORDER BY page_number", (page[0][0],))
    return [[{"kind": "text", "text": line, "size": 0, "bold": False} for line in body.splitlines() if line.strip()]
            for (body,) in texts]


def _own_language(section):
    return next((f[5:] for f in section.flags if f.startswith("lang:")), None)


def build(conn, document_id, *, pages=None, problems=None):
    """Make a new build of this document's sections. Returns its number, or None when the latest build already holds
    exactly these sections from this sectioner version."""
    doc = _rows(conn, "SELECT manufacturer_id, trade, equipment_type, doc_type, language, state FROM documents "
                      "WHERE id = %s", (document_id,))
    if not doc:
        raise UnknownTarget(f"No document {document_id}.")
    filters = dict(zip(FILTERS, doc[0][:5]))
    sections = build_sections(document_pages(conn, document_id, problems=problems) if pages is None else pages)
    own = "(SELECT substr(f, 6) FROM unnest(flags) f WHERE f LIKE 'lang:%%' LIMIT 1)"  # a section's own language wins
    _exec(conn, f"UPDATE sections SET manufacturer_id = %s, trade = %s, equipment_type = %s, doc_type = %s, "
                f"language = COALESCE({own}, %s) WHERE document_id = %s AND (manufacturer_id, trade, equipment_type, "
                f"doc_type, language) IS DISTINCT FROM (%s, %s, %s, %s, COALESCE({own}, %s))",
          (*filters.values(), document_id, *filters.values()))  # tags changed since a build: keep the copies true
    latest = _rows(conn, "SELECT COALESCE(MAX(build), 0) FROM sections WHERE document_id = %s", (document_id,))[0][0]
    if latest:
        before = _rows(conn, "SELECT body_sha, page_from, page_to, heading_path, kind, sectioner_version FROM sections "
                             "WHERE document_id = %s AND build = %s ORDER BY section_no", (document_id, latest))
        now = tuple((s.body_sha, s.page_from, s.page_to, s.heading_path, s.kind, SECTIONER_VERSION) for s in sections)
        if tuple(tuple(r) for r in before) == now:
            return None
    if not sections:
        return None
    number = latest + 1
    _exec(conn, "SAVEPOINT build_sections")
    try:
        for n, s in enumerate(sections, start=1):
            section_id = _rows(
                conn,
                "INSERT INTO sections (document_id, build, section_no, page_from, page_to, heading_path, kind, body, body_sha, "
                "token_count, flags, manufacturer_id, trade, equipment_type, doc_type, language, sectioner_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (document_id, number, n, s.page_from, s.page_to, s.heading_path, s.kind, s.body, s.body_sha, s.token_count,
                 list(s.flags), *list(filters.values())[:4], _own_language(s) or filters["language"],
                 SECTIONER_VERSION))[0][0]
            for term in sorted(terms(s.heading_path + "\n" + s.body)):
                _exec(conn, "INSERT INTO section_terms (term, section_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (term, section_id))
        _exec(conn, "UPDATE documents SET state = 'built', updated_at = (now() AT TIME ZONE 'UTC') "
                    "WHERE id = %s AND state IN ('new', 'tagged')", (document_id,))
    except Exception:
        _exec(conn, "ROLLBACK TO SAVEPOINT build_sections")
        raise
    _exec(conn, "RELEASE SAVEPOINT build_sections")
    return number


# ---------------------------------------------------------------- publish

def blocked(conn, document_id, lock=False):
    """Why this document may not go live, or None. lock=True holds the document and its manufacturer rows until the
    caller's transaction ends, so a hide in another session can't slip in between this check and the switch."""
    if lock:
        maker = _rows(conn, "SELECT manufacturer_id FROM documents WHERE id = %s FOR UPDATE", (document_id,))
        if maker and maker[0][0] is not None:
            _rows(conn, "SELECT 1 FROM manufacturers WHERE id = %s FOR SHARE", (maker[0][0],))
    row = _rows(conn, "SELECT d.hidden_at, d.doc_type, m.hidden_at, m.content_use FROM documents d "
                      "LEFT JOIN manufacturers m ON m.id = d.manufacturer_id WHERE d.id = %s", (document_id,))
    if not row:
        return "no such document"
    hidden, doc_type, maker_hidden, content_use = row[0]
    if hidden is not None:
        return "the document is hidden"
    if maker_hidden is not None:
        return "its manufacturer is hidden"
    if content_use == "off":
        return "its manufacturer's content is switched off"
    if doc_type is None:
        return "the document is not tagged yet"
    if doc_type == "not_equipment":
        return "the document is not about equipment"
    return None


def publish(conn, document_id, build=None):
    """(True, None) when the build (default: the newest) is live, else (False, why)."""
    reason = blocked(conn, document_id, lock=True)
    if reason:
        return False, reason
    if build is None:
        build = _rows(conn, "SELECT MAX(build) FROM sections WHERE document_id = %s", (document_id,))[0][0]
    if build is None or not _rows(conn, "SELECT 1 FROM sections WHERE document_id = %s AND build = %s LIMIT 1",
                                  (document_id, build)):
        return False, "it has no sections in that build"
    _exec(conn, "SAVEPOINT publish")
    try:
        _exec(conn, "UPDATE sections SET is_live = (build = %s) WHERE document_id = %s AND is_live IS DISTINCT FROM (build = %s)",
              (build, document_id, build))
        _exec(conn, "UPDATE documents SET live_build = %s, state = 'live', updated_at = (now() AT TIME ZONE 'UTC') "
                    "WHERE id = %s", (build, document_id))
        old = _rows(conn, "SELECT replaces_id FROM documents WHERE id = %s", (document_id,))[0][0]
        if old is not None:
            _exec(conn, "UPDATE sections SET is_live = false WHERE document_id = %s AND is_live", (old,))
            _exec(conn, "UPDATE documents SET state = 'retired', updated_at = (now() AT TIME ZONE 'UTC') WHERE id = %s", (old,))
    except Exception:
        _exec(conn, "ROLLBACK TO SAVEPOINT publish")
        raise
    _exec(conn, "RELEASE SAVEPOINT publish")
    return True, None


def build_and_publish(conn, *, brand_id=None, after_document=None):
    """Every tagged document whose sections are missing or from an older sectioner: build, then publish. A build that
    was held back earlier (hidden, switched off) is tried again, so it goes live once the reason is gone."""
    docs = _rows(conn,
                 "SELECT d.id FROM documents d WHERE d.state IN ('tagged', 'built', 'live') AND d.doc_type IS NOT NULL "
                 + ("AND EXISTS (SELECT 1 FROM document_urls du JOIN pages p ON p.id = du.page_id WHERE du.document_id = d.id "
                    "AND p.brand_id = %s) " if brand_id else "")
                 + "AND NOT EXISTS (SELECT 1 FROM sections s WHERE s.document_id = d.id AND s.sectioner_version = %s) "
                 "ORDER BY d.id", ((brand_id,) if brand_id else ()) + (SECTIONER_VERSION,))
    summary = {"built": 0, "live": 0, "held": 0, "empty": 0, "fallback": []}
    held = _rows(conn, "SELECT d.id FROM documents d WHERE d.state = 'built' AND EXISTS "
                       "(SELECT 1 FROM sections s WHERE s.document_id = d.id AND s.sectioner_version = %s)"
                 + (" AND EXISTS (SELECT 1 FROM document_urls du JOIN pages p ON p.id = du.page_id WHERE du.document_id = "
                    "d.id AND p.brand_id = %s)" if brand_id else "") + " ORDER BY d.id",
                 (SECTIONER_VERSION,) + ((brand_id,) if brand_id else ()))
    for (document_id,) in held:
        ok, _ = publish(conn, document_id)
        summary["live" if ok else "held"] += 1
        if after_document:
            after_document()
    for (document_id,) in docs:
        number = build(conn, document_id, problems=summary["fallback"])
        if number is None:
            summary["empty"] += 1
        else:
            summary["built"] += 1
            ok, _ = publish(conn, document_id, number)
            summary["live" if ok else "held"] += 1
        if after_document:
            after_document()
    return summary


# ---------------------------------------------------------------- hide / unhide

def _target(conn, target_type, target):
    if target_type == "document":
        found = _rows(conn, "SELECT id FROM documents WHERE id = %s", (int(target),)) if str(target).isdigit() else []
        if not found:
            raise UnknownTarget(f"No manual with id {target}.")
        return found[0][0]
    if target_type == "manufacturer":
        found = _rows(conn, "SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s", (alias_key(target),))
        if not found:
            raise UnknownTarget(f"No manufacturer called {target}.")
        return found[0][0]
    raise ValueError("hide a manual or a manufacturer")


def _log(conn, target_type, target_id, hidden, reason, by_whom):
    _exec(conn, "INSERT INTO visibility_log (target_type, target_id, hidden, reason, by_whom) VALUES (%s, %s, %s, %s, %s)",
          (target_type, target_id, hidden, reason, by_whom))


def hide(conn, target_type, target, *, reason, by_whom):
    target_id = _target(conn, target_type, target)
    if target_type == "document":
        _exec(conn, "UPDATE documents SET hidden_at = (now() AT TIME ZONE 'UTC'), hidden_reason = %s WHERE id = %s",
              (reason, target_id))
        _exec(conn, "UPDATE sections SET is_live = false WHERE document_id = %s AND is_live", (target_id,))
    else:
        _exec(conn, "UPDATE manufacturers SET hidden_at = (now() AT TIME ZONE 'UTC'), hidden_reason = %s WHERE id = %s",
              (reason, target_id))
        _exec(conn, "UPDATE sections SET is_live = false WHERE manufacturer_id = %s AND is_live", (target_id,))
    _log(conn, target_type, target_id, True, reason, by_whom)
    return target_id


def unhide(conn, target_type, target, *, reason, by_whom):
    target_id = _target(conn, target_type, target)
    if target_type == "document":
        _exec(conn, "UPDATE documents SET hidden_at = NULL, hidden_reason = NULL WHERE id = %s", (target_id,))
        docs = [target_id]
    else:
        _exec(conn, "UPDATE manufacturers SET hidden_at = NULL, hidden_reason = NULL WHERE id = %s", (target_id,))
        docs = [r[0] for r in _rows(conn, "SELECT id FROM documents WHERE manufacturer_id = %s", (target_id,))]
    for document_id in docs:
        live = _rows(conn, "SELECT live_build FROM documents WHERE id = %s AND state = 'live'", (document_id,))
        if live and live[0][0] is not None and blocked(conn, document_id, lock=True) is None:
            _exec(conn, "UPDATE sections SET is_live = true WHERE document_id = %s AND build = %s", (document_id, live[0][0]))
    _log(conn, target_type, target_id, False, reason, by_whom)
    return target_id


# ---------------------------------------------------------------- check

def check(conn):
    """Read only. Problems (document ids) and counts of live documents and sections per manufacturer and trade."""
    return {
        "live_without_sections": [r[0] for r in _rows(
            conn, "SELECT d.id FROM documents d WHERE d.state = 'live' AND d.hidden_at IS NULL AND NOT EXISTS "
                  "(SELECT 1 FROM sections s WHERE s.document_id = d.id AND s.is_live) ORDER BY d.id")],
        "hidden_but_live": [r[0] for r in _rows(
            conn, "SELECT DISTINCT s.document_id FROM sections s JOIN documents d ON d.id = s.document_id "
                  "LEFT JOIN manufacturers m ON m.id = d.manufacturer_id WHERE s.is_live AND (d.hidden_at IS NOT NULL "
                  "OR m.hidden_at IS NOT NULL OR m.content_use = 'off' OR d.doc_type = 'not_equipment') ORDER BY 1")],
        "live_but_not_current": [r[0] for r in _rows(
            conn, "SELECT DISTINCT s.document_id FROM sections s JOIN documents d ON d.id = s.document_id "
                  "WHERE s.is_live AND (d.state <> 'live' OR s.build IS DISTINCT FROM d.live_build) ORDER BY 1")],
        "likely_duplicates": [tuple(r) for r in _rows(
            conn, "SELECT a.id, b.id FROM documents a JOIN documents b ON b.manufacturer_id = a.manufacturer_id "
                  "AND b.title = a.title AND b.id > a.id WHERE a.state = 'live' AND b.state = 'live' AND "
                  "(SELECT array_agg(model_key ORDER BY model_key) FROM document_models WHERE document_id = a.id) IS NOT DISTINCT "
                  "FROM (SELECT array_agg(model_key ORDER BY model_key) FROM document_models WHERE document_id = b.id) "
                  "ORDER BY 1, 2")],
        "needs_ocr": _rows(conn, "SELECT COUNT(*) FROM documents WHERE needs_ocr")[0][0],
        "live_without_vectors": _rows(  # P13: Breeze's meaning search cannot find these yet (text search can)
            conn, "SELECT COUNT(*) FROM sections s WHERE s.is_live AND NOT EXISTS "
                  "(SELECT 1 FROM section_vectors_te3s_1536 v WHERE v.section_id = s.id)")[0][0],
        "counts": [tuple(r) for r in _rows(
            conn, "SELECT COALESCE(m.name, '(none)'), COALESCE(d.trade, '(none)'), COUNT(DISTINCT d.id), COUNT(s.id) "
                  "FROM documents d LEFT JOIN manufacturers m ON m.id = d.manufacturer_id "
                  "LEFT JOIN sections s ON s.document_id = d.id AND s.is_live WHERE d.state = 'live' "
                  "GROUP BY 1, 2 ORDER BY 1, 2")],
    }


# ---------------------------------------------------------------- prune (user, 2026-10-27: clean up while nothing is live)

_STALE = ("SELECT s.id FROM sections s JOIN documents d ON d.id = s.document_id WHERE "
          "d.state = 'retired' "  # a replaced version: its replacement is the live one
          "OR (d.live_build IS NOT NULL AND s.build < d.live_build) "  # older than the live build; newer ones may be on their way
          "OR (d.live_build IS NULL AND s.build < (SELECT MAX(build) FROM sections n WHERE n.document_id = d.id))")


def prune(conn):
    """Delete section builds that can never be live again, with their terms. Needs the migrate role: the runtime role
    cannot delete. Returns {"sections": n, "terms": n, "vectors": n}. Builds can always be made again from the kept
    originals. Run embed before prune: a rebuilt section reuses its old twin's vector only while the twin is kept."""
    vectors = _rows(conn, f"WITH gone AS (DELETE FROM section_vectors_te3s_1536 WHERE section_id IN ({_STALE}) "
                          "RETURNING 1) SELECT COUNT(*) FROM gone")[0][0]
    terms = _rows(conn, f"WITH gone AS (DELETE FROM section_terms WHERE section_id IN ({_STALE}) RETURNING 1) "
                        "SELECT COUNT(*) FROM gone")[0][0]
    sections = _rows(conn, f"WITH gone AS (DELETE FROM sections WHERE id IN ({_STALE}) RETURNING 1) "
                           "SELECT COUNT(*) FROM gone")[0][0]
    return {"sections": sections, "terms": terms, "vectors": vectors}
