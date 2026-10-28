"""Free discovery: `Sitemap:` lines in an approved host's cached robots.txt, read through the safe fetcher."""
import hashlib
import re
from urllib.parse import urlsplit

from defusedxml import ElementTree

from crawler.brands import UnknownTarget, find_brand
from crawler.hosts import in_scope, normalize_host

MAX_SITEMAP_URLS = 5000
_SITEMAP_LINE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _locs(body, max_urls):
    """(kind, [loc, ...]) from sitemap XML: kind is 'urlset' or 'index'. Anything odd gives (None, [])."""
    try:
        root = ElementTree.fromstring(body, forbid_dtd=True)
    except Exception:  # not XML, a DTD, entities, or a bomb
        return None, []
    kind = {"urlset": "urlset", "sitemapindex": "index"}.get(root.tag.rsplit("}", 1)[-1])
    if kind is None:
        return None, []
    locs = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "loc" and node.text and node.text.strip():
            locs.append(node.text.strip())
            if len(locs) >= max_urls:
                break
    return kind, locs


def _host_and_path(url):
    try:
        parts = urlsplit(url)
        return normalize_host(parts.hostname or ""), parts.path or "/"
    except ValueError:
        return None, None


def read_sitemaps(conn, fetcher, brand_name, *, max_urls=MAX_SITEMAP_URLS):
    brand_id = find_brand(conn, brand_name)
    if brand_id is None:
        raise UnknownTarget(f"No brand called {brand_name}.")
    approved = [r[0] for r in _rows(
        conn, "SELECT host FROM brand_domains WHERE brand_id = %s AND status = 'approved'", (brand_id,))]
    sections = []
    for (start,) in _rows(conn, "SELECT url FROM start_urls WHERE brand_id = %s AND status = 'active'", (brand_id,)):
        host, path = _host_and_path(start)
        folder = path[: path.rfind("/") + 1]  # the start URL's own folder; a site's top folder is too wide to use
        if host and folder not in ("", "/"):
            sections.append((host, folder))
    if not sections:
        return 0

    sitemap_urls = []
    for host in approved:
        rows = _rows(conn, "SELECT robots_body, robots_status FROM hosts WHERE host = %s", (host,))
        if rows and rows[0][0] and rows[0][1] is not None and 200 <= rows[0][1] < 300:
            sitemap_urls += _SITEMAP_LINE.findall(rows[0][0])

    added = 0

    def take(locs):
        nonlocal added
        for loc in locs:
            host, path = _host_and_path(loc)
            if (host is None or len(loc) > 2048 or not in_scope(loc, approved)
                    or not any(host == h and path.startswith(p) for h, p in sections)):
                continue
            with conn.cursor() as cur:
                # a listed page is its own start, so one link deep reaches its documents (D60)
                cur.execute("INSERT INTO start_urls (brand_id, url, url_sha, found_by, depth) VALUES (%s, %s, %s, 'sitemap', 1) "
                            "ON CONFLICT (brand_id, url_sha) DO NOTHING",
                            (brand_id, loc, hashlib.sha256(loc.encode("utf-8")).hexdigest()))
                added += 1 if cur.rowcount == 1 else 0

    def read(url):
        if not in_scope(url, approved):
            return None, []
        result = fetcher.fetch(url, brand_id, xml=True)
        if result.outcome != "fetched":
            return None, []
        return _locs(result.body, max_urls)

    seen = set()
    for url in dict.fromkeys(sitemap_urls):
        kind, locs = read(url)
        if kind == "urlset":
            take(locs)
        elif kind == "index":
            for child in locs:  # one level down only; an index inside an index is looked at but not followed
                if child not in seen:
                    seen.add(child)
                    child_kind, child_locs = read(child)
                    if child_kind == "urlset":
                        take(child_locs)
    return added
