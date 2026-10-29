"""Brands, start URLs and host approval. Nothing here commits; the caller does."""
import hashlib
from urllib.parse import urlsplit

from crawler.hosts import RefusedHost, is_refused_host, normalize_host
from crawler.rules import is_valid_keyword
from crawler.store import HAS_NO_DOCUMENT_SQL, alias_key

MAX_DEPTH = 10


class UnknownTarget(Exception):
    pass


class HostUnknown(Exception):
    pass


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur


def brand_key(name):
    return " ".join(name.split()).lower()


def find_brand(conn, name):
    rows = _rows(conn, "SELECT id FROM brands WHERE name_key = %s", (brand_key(name),))
    return rows[0][0] if rows else None


def _brand_or_fail(conn, name):
    brand_id = find_brand(conn, name)
    if brand_id is None:
        raise UnknownTarget(f"No brand called {name}.")
    return brand_id


def add_brand(conn, name):
    name = " ".join(name.split())
    if not name:
        raise ValueError("A brand needs a name.")
    return _rows(
        conn,
        "INSERT INTO brands (name, name_key) VALUES (%s, %s) "
        "ON CONFLICT (name_key) DO UPDATE SET name_key = EXCLUDED.name_key RETURNING id",
        (name, brand_key(name)),
    )[0][0]


def add_start_url(conn, brand_name, url, depth=None, kind="page"):
    """A start address for the brand. depth: how many links deep to follow (default 3, D60). kind: 'page' or 'feed'."""
    brand_id = _brand_or_fail(conn, brand_name)
    if depth is not None and not (isinstance(depth, int) and 0 <= depth <= MAX_DEPTH):
        raise ValueError(f"Depth must be a whole number from 0 to {MAX_DEPTH}.")
    if kind not in ("page", "feed"):
        raise ValueError("A start address is a page or a feed.")
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        ok = parts.scheme.lower() in ("http", "https") and bool(parts.hostname) and len(url) <= 2048
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{url or '(empty)'} is not a web address starting with http:// or https://")
    return _rows(
        conn,
        "INSERT INTO start_urls (brand_id, url, url_sha, found_by, depth, kind) VALUES (%s, %s, %s, 'user', COALESCE(%s, 3), %s) "
        "ON CONFLICT (brand_id, url_sha) DO UPDATE SET depth = COALESCE(%s, start_urls.depth), kind = EXCLUDED.kind "
        "RETURNING id",
        (brand_id, url, hashlib.sha256(url.encode("utf-8")).hexdigest(), depth, kind, depth),
    )[0][0]


def approve_host(conn, host, brand=None, ignore_robots=None, use_browser=None):
    """Approve a host for fetching. ignore_robots / use_browser flip the host's switches (D67, D68a); None leaves them."""
    host = normalize_host(host)
    if is_refused_host(host):
        raise RefusedHost(f"{host} is not allowed. Refused.")
    if brand is not None:
        brand_id = _brand_or_fail(conn, brand)
        _exec(
            conn,
            "INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, %s, 'approved', 'user') "
            "ON CONFLICT (brand_id, host) DO UPDATE SET status = 'approved'",
            (brand_id, host),
        )
    else:
        if not _rows(conn, "SELECT 1 FROM brand_domains WHERE host = %s", (host,)):
            raise HostUnknown(f"No brand has seen {host}. Add --brand <name>.")
        _exec(conn, "UPDATE brand_domains SET status = 'approved' WHERE host = %s AND status <> 'approved'", (host,))
    if ignore_robots is not None or use_browser is not None:
        _exec(conn, "INSERT INTO hosts (host) VALUES (%s) ON CONFLICT (host) DO NOTHING", (host,))
    if ignore_robots is not None:
        _exec(conn, "UPDATE hosts SET ignore_robots = %s, ignore_robots_changed_at = (now() AT TIME ZONE 'UTC') "
                    "WHERE host = %s AND ignore_robots <> %s", (ignore_robots, host, ignore_robots))
    if use_browser is not None:
        _exec(conn, "UPDATE hosts SET use_browser = %s WHERE host = %s", (use_browser, host))
    return host


def verdict(conn, url, decision):
    """Your decision on one address, for every brand that has seen it; it beats keywords, rules and the AI.
    Returns how many page rows changed."""
    if decision not in ("crawl", "skip"):
        raise ValueError("A verdict is crawl or skip.")
    url = (url or "").strip()
    sha = hashlib.sha256(url.encode("utf-8")).hexdigest()
    state = "queued" if decision == "crawl" else "skipped_user"
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET decision = %s, decided_by = 'user', decision_reason = 'your verdict', "
                    "fetch_state = CASE WHEN fetched_at IS NULL THEN %s ELSE fetch_state END WHERE url_sha = %s",
                    (decision, state, sha))
        changed = cur.rowcount
    if not changed:
        raise UnknownTarget(f"The crawler has never seen {url}.")
    return changed


def set_keyword(conn, keyword, effect, *, brand=None, active=True, note=None):
    """Add (or switch back on) a skip or prefer keyword, or switch one off. Rows are never removed (D8)."""
    keyword = (keyword or "").strip().lower()
    if not is_valid_keyword(keyword):
        raise ValueError(f"{keyword or '(empty)'} is not a keyword: one word of letters, digits, - or _, "
                         "optionally ending in *.")
    brand_id = _brand_or_fail(conn, brand) if brand is not None else None
    if active:
        _exec(conn, "INSERT INTO crawl_keywords (keyword, effect, brand_id, source, note) VALUES (%s, %s, %s, 'user', %s) "
                    "ON CONFLICT ON CONSTRAINT crawl_keywords_one_rule DO UPDATE SET active = true, effect = EXCLUDED.effect, "
                    "note = COALESCE(EXCLUDED.note, crawl_keywords.note)", (keyword, effect, brand_id, note))
        return keyword
    with conn.cursor() as cur:
        cur.execute("UPDATE crawl_keywords SET active = false WHERE keyword = %s AND effect = %s AND host IS NULL "
                    "AND brand_id IS NOT DISTINCT FROM %s", (keyword, effect, brand_id))
        if not cur.rowcount:
            raise UnknownTarget(f"No {effect} keyword {keyword}" + (f" for {brand}." if brand else "."))
    return keyword


def list_keywords(conn, effect):
    return _rows(conn, "SELECT k.keyword, k.active, k.source, COALESCE(b.name, 'all brands'), k.host, k.note "
                       "FROM crawl_keywords k LEFT JOIN brands b ON b.id = k.brand_id WHERE k.effect = %s "
                       "ORDER BY k.source DESC, k.keyword", (effect,))


def stop(conn, target):
    brand_id = find_brand(conn, target)
    if brand_id is not None:
        _exec(conn, "UPDATE brands SET status = 'stopped' WHERE id = %s", (brand_id,))
        _exec(conn, "UPDATE start_urls SET status = 'stopped' WHERE brand_id = %s", (brand_id,))
        return "brand"
    try:
        host = normalize_host(target)
    except ValueError:
        host = None
    if host is not None and _rows(conn, "SELECT 1 FROM brand_domains WHERE host = %s", (host,)):
        _exec(conn, "UPDATE brand_domains SET status = 'stopped' WHERE host = %s", (host,))
        return "host"
    raise UnknownTarget(f"No brand or host called {target}.")


def resume(conn, target):
    """Undo stop: a brand and its stopped start addresses go back to active; a stopped host is approved again (as ok)."""
    brand_id = find_brand(conn, target)
    if brand_id is not None:
        _exec(conn, "UPDATE brands SET status = 'active' WHERE id = %s", (brand_id,))
        _exec(conn, "UPDATE start_urls SET status = 'active' WHERE brand_id = %s AND status = 'stopped'", (brand_id,))
        return "brand"
    try:
        host = normalize_host(target)
    except ValueError:
        host = None
    if host is not None and _rows(conn, "SELECT 1 FROM brand_domains WHERE host = %s", (host,)):
        _exec(conn, "UPDATE brand_domains SET status = 'approved' WHERE host = %s AND status = 'stopped'", (host,))
        return "host"
    raise UnknownTarget(f"No brand or host called {target}.")


def status(conn):
    rows = _rows(
        conn,
        "SELECT b.name, b.status, "
        "(SELECT COUNT(*) FROM brand_domains d WHERE d.brand_id = b.id AND d.status = 'waiting'), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND p.fetched_at IS NOT NULL "
        " AND p.http_status < 400 AND p.fail_count = 0), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND p.fetched_at IS NULL "
        " AND p.http_status IS NULL AND p.fail_count = 0 AND p.fetch_state = 'queued'), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND (p.http_status >= 400 OR p.fail_count > 0)), "
        "(SELECT COUNT(DISTINCT dm.model_id) FROM pages p JOIN document_urls du ON du.page_id = p.id "
        " JOIN document_models dm ON dm.document_id = du.document_id WHERE p.brand_id = b.id), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND p.ai_batch_id = 'submitting') "
        "FROM brands b ORDER BY b.name",
    )
    keys = ("brand", "state", "hosts_waiting", "pages_done", "pages_waiting", "pages_failed", "models_found", "pages_submitting")
    result = [dict(zip(keys, (r[0], r[1], *(int(v) for v in r[2:])))) for r in rows]
    for row in result:
        key = alias_key(row["brand"])
        row["documents"], row["untagged"] = (int(n) for n in _rows(
            conn,
            "SELECT COUNT(DISTINCT d.id), COUNT(DISTINCT d.id) FILTER (WHERE d.doc_type IS NULL) FROM pages p "
            "JOIN document_urls du ON du.page_id = p.id JOIN documents d ON d.id = du.document_id "
            "JOIN brands b ON b.id = p.brand_id WHERE b.name = %s", (row["brand"],))[0])
        brand_id = find_brand(conn, row["brand"])
        row["starts_at_limit"] = int(_rows(
            conn, "SELECT COUNT(*) FROM (SELECT DISTINCT ON (start_url_id) limit_hit FROM crawl_runs WHERE brand_id = %s "
                  "ORDER BY start_url_id, started_at DESC) last WHERE limit_hit", (brand_id,))[0][0])
        row["pdf_links_beyond_depth"] = int(_rows(
            conn, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND fetch_state = 'beyond_depth' "
                  "AND split_part(lower(url), '?', 1) LIKE '%%.pdf'", (brand_id,))[0][0])
        row["waiting_for_ai"] = int(_rows(
            conn, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND fetch_state = 'ask_ai'", (brand_id,))[0][0])
        row["keyword_skips"] = {reason: int(n) for reason, n in _rows(
            conn, "SELECT decision_reason, COUNT(*) FROM pages WHERE brand_id = %s AND fetch_state = 'skipped_keyword' "
                  "GROUP BY decision_reason ORDER BY 2 DESC, 1", (brand_id,))}
        row["unreadable"] = int(_rows(
            conn, "SELECT COUNT(DISTINCT d.id) FROM documents d JOIN document_urls du ON du.document_id = d.id "
                  "JOIN pages p ON p.id = du.page_id WHERE p.brand_id = %s AND d.page_count = 0", (brand_id,))[0][0])
        row["needs_ocr"], row["pages_without_text"] = (int(n) for n in _rows(
            conn, "SELECT COUNT(*), COALESCE(SUM(page_count - text_pages), 0) FROM documents d WHERE d.needs_ocr AND "
                  "EXISTS (SELECT 1 FROM document_urls du JOIN pages p ON p.id = du.page_id WHERE du.document_id = d.id "
                  "AND p.brand_id = %s)", (brand_id,))[0])
        row["failed_reasons"] = {reason: int(n) for reason, n in _rows(
            conn, "SELECT fail_reason, COUNT(*) FROM pages WHERE brand_id = %s AND fail_count > 0 "
                  "GROUP BY fail_reason ORDER BY 2 DESC, 1", (brand_id,))}
        row["suggested_keywords"] = [r[0] for r in _rows(
            conn, "SELECT s.keyword FROM keyword_suggestions s WHERE s.brand_id = %s AND NOT EXISTS (SELECT 1 FROM "
                  "crawl_keywords k WHERE k.keyword = s.keyword AND (k.brand_id IS NULL OR k.brand_id = s.brand_id)) "
                  "ORDER BY s.times DESC, s.keyword LIMIT 10", (brand_id,))]
        row["es_models_without_documents"] = int(_rows(
            conn,
            "SELECT COUNT(*) FROM models em WHERE em.energy_star_confirmed AND em.manufacturer_id IN "
            "(SELECT id FROM manufacturers WHERE name_key = %s UNION "
            " SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s) "
            "AND " + HAS_NO_DOCUMENT_SQL, (key, key))[0][0])
    return result
