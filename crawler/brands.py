"""Brands, start URLs and host approval. Nothing here commits; the caller does."""
import hashlib
from urllib.parse import urlsplit

from crawler.hosts import RefusedHost, is_refused_host, normalize_host
from crawler.store import HAS_NO_SPECS_SQL, alias_key


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
    cur = _exec(
        conn,
        "INSERT INTO brands (name, name_key) VALUES (%s, %s) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
        (name, brand_key(name)),
    )
    return cur.lastrowid


def add_start_url(conn, brand_name, url):
    brand_id = _brand_or_fail(conn, brand_name)
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        ok = parts.scheme.lower() in ("http", "https") and bool(parts.hostname) and len(url) <= 2048
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{url or '(empty)'} is not a web address starting with http:// or https://")
    cur = _exec(
        conn,
        "INSERT INTO start_urls (brand_id, url, url_sha, found_by) VALUES (%s, %s, %s, 'user') "
        "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
        (brand_id, url, hashlib.sha256(url.encode("utf-8")).hexdigest()),
    )
    return cur.lastrowid


def approve_host(conn, host, brand=None):
    host = normalize_host(host)
    if is_refused_host(host):
        raise RefusedHost(f"{host} is not allowed. Refused.")
    if brand is not None:
        brand_id = _brand_or_fail(conn, brand)
        _exec(
            conn,
            "INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, %s, 'approved', 'user') "
            "ON DUPLICATE KEY UPDATE status = 'approved'",
            (brand_id, host),
        )
        return host
    if not _rows(conn, "SELECT 1 FROM brand_domains WHERE host = %s", (host,)):
        raise HostUnknown(f"No brand has seen {host}. Add --brand <name>.")
    _exec(conn, "UPDATE brand_domains SET status = 'approved' WHERE host = %s AND status <> 'approved'", (host,))
    return host


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


def status(conn):
    rows = _rows(
        conn,
        "SELECT b.name, b.status, "
        "(SELECT COUNT(*) FROM brand_domains d WHERE d.brand_id = b.id AND d.status = 'waiting'), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND p.fetched_at IS NOT NULL "
        " AND p.http_status < 400 AND p.fail_count = 0), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND p.fetched_at IS NULL "
        " AND p.http_status IS NULL AND p.fail_count = 0), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND (p.http_status >= 400 OR p.fail_count > 0)), "
        "(SELECT COUNT(*) FROM found_models m WHERE m.brand_id = b.id), "
        "(SELECT COUNT(*) FROM pages p WHERE p.brand_id = b.id AND p.ai_batch_id = 'submitting') "
        "FROM brands b ORDER BY b.name",
    )
    keys = ("brand", "state", "hosts_waiting", "pages_done", "pages_waiting", "pages_failed", "models_found", "pages_submitting")
    result = [dict(zip(keys, (r[0], r[1], *(int(v) for v in r[2:])))) for r in rows]
    for row in result:
        key = alias_key(row["brand"])
        row["es_models_without_specs"] = int(_rows(
            conn,
            "SELECT COUNT(*) FROM equipment_models em WHERE em.energy_star_confirmed = 1 AND em.manufacturer_id IN "
            "(SELECT id FROM manufacturers WHERE name_key = %s UNION "
            " SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s) "
            "AND " + HAS_NO_SPECS_SQL, (key, key))[0][0])
    return result
