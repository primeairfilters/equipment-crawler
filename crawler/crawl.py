"""run: follow links from each active brand's start URLs, inside the limits."""
from collections import deque
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from crawler.brands import UnknownTarget, find_brand
from crawler.textextract import index_page


def extract_links(body, base_url):
    """Absolute http(s) links found in an HTML page, fragments removed."""
    links = []
    for node in LexborHTMLParser(body).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href:
            continue
        try:
            url = urldefrag(urljoin(base_url, href))[0]
            if urlsplit(url).scheme in ("http", "https"):
                links.append(url)
        except ValueError:
            continue
    return links


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def run(conn, fetcher, *, brand=None, max_pages=2000, max_depth=2, after_page=None):
    summary = {"fetched": 0, "not_modified": 0, "failed": 0, "refused": 0}
    if brand is not None:
        brand_id = find_brand(conn, brand)
        if brand_id is None:
            raise UnknownTarget(f"No brand called {brand}.")
        brands = _rows(conn, "SELECT id FROM brands WHERE id = %s AND status = 'active'", (brand_id,))
    else:
        brands = _rows(conn, "SELECT id FROM brands WHERE status = 'active' ORDER BY id")

    for (brand_id,) in brands:
        seen = set()
        starts = _rows(conn, "SELECT id, url FROM start_urls WHERE brand_id = %s AND status = 'active' ORDER BY id",
                       (brand_id,))
        for start_id, start_url in starts:
            queue = deque([(urldefrag(start_url)[0], 0, None)])
            attempts = 0
            while queue and attempts < max_pages:
                url, depth, parent_id = queue.popleft()
                if url in seen:
                    continue
                seen.add(url)
                due, page_id = fetcher.due_info(brand_id, url)
                links = []
                if not due:
                    links = _known_children(conn, brand_id, page_id)
                else:
                    result = fetcher.fetch(url, brand_id, depth=depth, start_url_id=start_id, parent_id=parent_id)
                    summary[result.outcome] += 1
                    if result.outcome != "refused":
                        attempts += 1
                    page_id = result.page_id
                    if result.outcome == "fetched":
                        index_page(conn, page_id, result.body, is_pdf=result.is_pdf)
                        if not result.is_pdf:
                            links = extract_links(result.body, result.url)
                    elif result.outcome == "not_modified":
                        links = _known_children(conn, brand_id, page_id)
                    if after_page is not None:
                        after_page()
                if depth < max_depth and page_id is not None:
                    for link in links:
                        if link not in seen:
                            queue.append((link, depth + 1, page_id))
    return summary


def _known_children(conn, brand_id, page_id):
    """Pages already found below this one, so a page that is not read again still leads to its children."""
    if page_id is None:
        return []
    return [r[0] for r in _rows(conn, "SELECT url FROM pages WHERE brand_id = %s AND parent_id = %s ORDER BY id",
                                (brand_id, page_id))]
