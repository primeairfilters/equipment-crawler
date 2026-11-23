"""P19 (D75, D76; Q22): the admin page's brands (control.brands) reach the crawler, and a brand name is enough.

- sync: at the start of every `auto`, each page row is linked to a crawler brand (made if new) and its controls are
  copied: active runs, paused and archived are stopped; the monthly budget, the host-approval switch and the
  brand's trades (P23; empty = any trade).
- take_start_pages: pages the user added on the Hosts tab (control.start_pages, user 2026-11-13) become start
  addresses of their brand; the host is approved for all its pages and recorded as the user's decision, overruling
  the host judge. One that cannot be taken keeps why (refused).
- discover_brand: a brand never discovered is asked about once (crawler/discover.py, one live web-search call); of
  the hosts it names, the brand's own site (own_site: its name is in the address) is approved at once for all its
  pages, recorded as a 'rule' decision. Every other host waits for the host judge (P18), which only ever approves PDFs.

Nothing here commits; the caller does.
"""
import json
import re
from urllib.parse import urlsplit

from crawler import brands as crawler_brands
from crawler.hosts import is_refused_host, normalize_host
from crawler.hostjudge import enabled, never_asked

OWN_SITE = "the brand's own site: its name is in the address"
MIN_WORD = 4  # a brand's first word this long names its site ("mitsubishi" in mitsubishicomfort.com)


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def sync(conn):
    """Link every page row to a crawler brand and copy its controls. Returns how many rows were newly linked."""
    linked = 0
    for control_id, name in _rows(conn, "SELECT id, name FROM control.brands WHERE crawl_brand_id IS NULL ORDER BY id"):
        brand_id = crawler_brands.add_brand(conn, name)  # an existing brand of that name is found, not made again
        _rows(conn, "UPDATE control.brands SET crawl_brand_id = %s WHERE id = %s", (brand_id, control_id))
        linked += 1
    for brand_id, state, status in _rows(conn, "SELECT b.id, c.state, b.status FROM control.brands c JOIN brands b "
                                               "ON b.id = c.crawl_brand_id"):
        active = state == "active"
        if active != (status == "active"):
            crawler_brands.set_active(conn, brand_id, active)
    _rows(conn, "UPDATE brands b SET monthly_budget_usd = c.monthly_budget_usd, auto_hosts = c.auto_hosts, "
                "trades = c.trades FROM control.brands c WHERE c.crawl_brand_id = b.id AND (b.monthly_budget_usd, "
                "b.auto_hosts, b.trades) IS DISTINCT FROM (c.monthly_budget_usd, c.auto_hosts, c.trades)")
    take_start_pages(conn)
    return linked


def find_websites(conn, client, *, now=None, cap_usd=None, after_brand=None, time_up=None):
    """LIVE_CAMERA_CIRCLE D14 (the user, 2026-11-22): look up where brands publish, first thing in a run and in this
    order, until the cap is short: (1) new brands Breeze missed, (2) brands never looked up, (3) the 90-day redo of
    brands looked up before (discover.REDO_AFTER; it never ran from the nightly run before this). Active brands within
    their own monthly budget only. A brand with a start page the user typed and never looked up is still not asked
    (P19). Returns [(name, decided, waiting)] for the run report."""
    from datetime import datetime, timezone

    from crawler.discover import REDO_AFTER
    from crawler.misses import ADDED_BY_MISS
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    due = _rows(conn, "SELECT b.id, b.name FROM brands b LEFT JOIN control.brands c ON c.crawl_brand_id = b.id "
                      "WHERE b.status = 'active' AND ((b.discovered_at IS NULL AND NOT EXISTS (SELECT 1 FROM start_urls s "
                      "WHERE s.brand_id = b.id)) OR b.discovered_at <= %s) AND (SELECT COALESCE(SUM(a.cost_usd), 0) "
                      "FROM ai_calls a WHERE a.brand_id = b.id AND a.created_at >= %s) < b.monthly_budget_usd "
                      "ORDER BY CASE WHEN b.discovered_at IS NULL AND c.added_by = %s THEN 0 "
                      "WHEN b.discovered_at IS NULL THEN 1 ELSE 2 END, b.discovered_at, b.created_at, b.id",
                (now - REDO_AFTER, month, ADDED_BY_MISS))
    report = []
    for brand_id, name in due:
        if time_up is not None and time_up():
            break
        outcome = []
        decided, waiting = discover_brand(conn, client, brand_id, name, now=now, cap_usd=cap_usd, outcome=outcome)
        if after_brand is not None:
            after_brand()
        if outcome == ["budget"]:
            break
        if outcome == ["found"]:
            report.append((name, decided, waiting))
    return report


USER_PAGE = "you added a page on it"
NEVER_CRAWL = "That website is on the never-crawl list."


def take_start_pages(conn):
    """Each page the user added and no run has taken yet: a start address of its brand, its host approved for all its
    pages and the user's host decision. Returns how many were taken."""
    taken = 0
    for page_id, url, brand_id, name in _rows(
            conn, "SELECT sp.id, sp.url, b.id, b.name FROM control.start_pages sp "
                  "JOIN control.brands c ON c.id = sp.brand_id JOIN brands b ON b.id = c.crawl_brand_id "
                  "WHERE sp.start_url_id IS NULL AND sp.refused IS NULL ORDER BY sp.id"):
        try:
            host = normalize_host(urlsplit(url).hostname or "")
        except ValueError:
            host = None
        if host is None or is_refused_host(host):
            why = NEVER_CRAWL if host else "That is not a web address the crawler can open."
            _rows(conn, "UPDATE control.start_pages SET refused = %s WHERE id = %s", (why, page_id))
            continue
        with conn.transaction():  # a savepoint: a page that fails leaves nothing half done
            try:
                start_id = crawler_brands.add_start_url(conn, name, url)
            except ValueError as exc:
                start_id, why = None, str(exc)
            if start_id is not None:
                crawler_brands.approve_host(conn, host, brand=name)
                _rows(conn, "UPDATE brand_domains SET scope = 'all' WHERE brand_id = %s AND host = %s", (brand_id, host))
                _rows(conn, "INSERT INTO host_decisions (brand_id, host, state, decided_by, reason) "
                            "VALUES (%s, %s, 'confirmed', 'user', %s) ON CONFLICT (brand_id, host) DO UPDATE SET "
                            "state = 'confirmed', decided_by = 'user', reason = EXCLUDED.reason, "
                            "updated_at = (now() AT TIME ZONE 'UTC')", (brand_id, host, USER_PAGE))
                _rows(conn, "UPDATE control.start_pages SET start_url_id = %s WHERE id = %s", (start_id, page_id))
                taken += 1
            else:
                _rows(conn, "UPDATE control.start_pages SET refused = %s WHERE id = %s", (why[:500], page_id))
    return taken


def own_site(brand, host):
    """True when the host is plainly the brand's: the brand's first word of MIN_WORD+ letters is in one of its names
    (www.ruud.com, www.americanstandardair.com), or for a short brand (LG) one name is the brand exactly."""
    if never_asked(host):
        return False
    labels = host.lower().split(".")[:-1]
    words = [w for w in (re.sub(r"[^a-z0-9]", "", w) for w in brand.lower().split()) if w]
    long = [w for w in words if len(w) >= MIN_WORD]
    if long:
        return any(long[0] in label for label in labels)
    return bool(words) and "".join(words) in labels


def discover_brand(conn, client, brand_id, name, *, now=None, cap_usd=None, outcome=None):
    """Ask where the brand publishes and approve its own site. Returns [(host, state, reason)] for the report, and
    [host] of the hosts found that wait for the user (automatic approval off). `outcome`, a list, gets what discover
    said ("budget", "recent", "unusable" or "found"), so a caller looking up many brands can stop at the cap."""
    from crawler.discover import discover
    found = discover(conn, client, name, now=now, cap_usd=cap_usd)
    if outcome is not None:
        outcome.append(found if isinstance(found, str) else "found")
    if not isinstance(found, dict):
        return [], []
    waiting = [h for (h,) in _rows(conn, "SELECT host FROM brand_domains WHERE brand_id = %s AND status = 'waiting' "
                                         "AND found_by = 'ai' ORDER BY host", (brand_id,))]
    if not enabled(conn, brand_id):
        return [], waiting
    out = []
    for host in waiting:
        if not own_site(name, host):
            continue
        _rows(conn, "UPDATE brand_domains SET status = 'approved', scope = 'all' WHERE brand_id = %s AND host = %s",
              (brand_id, host))
        _rows(conn, "INSERT INTO host_decisions (brand_id, host, state, decided_by, reason, evidence) "
                    "VALUES (%s, %s, 'confirmed', 'rule', %s, %s) ON CONFLICT (brand_id, host) DO NOTHING",
              (brand_id, host, OWN_SITE, json.dumps({"found_by": "discovery"})))
        out.append((host, "own", OWN_SITE))
    return out, []
