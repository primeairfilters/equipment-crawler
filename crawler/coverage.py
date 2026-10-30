"""P20 (D76; Q24): how much of a brand the crawler has, and going after the rest.

- pull_models: the brand's public model list from the ENERGY STAR Model Index (every certified product, every
  category), once every PULL_EVERY per brand, through the fetcher's polite JSON GET. Rows are kept in public_models
  and saved as the brand's models (energy_star_confirmed), which is the list gap-fill searches for.
- measure: coverage = model families (gapfill.family) with at least one manual, overall and per category; one
  crawl.coverage row per brand per run, with what the round did, and its phase: no_public_list, filling_gaps or
  upkeep (a round with no new manual, no search made and nothing queued).
- Site search: record_search_form notes a brand page's own search form (a GET form with a query box, posting to the
  same site); site_search then searches it for uncovered families, a few each run, each family once every
  SEARCH_AGAIN_AFTER, as start addresses crawled like any other (robots.txt and the polite gap hold).

Nothing here commits; callers do.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urljoin, urlsplit

from crawler.gapfill import family
from crawler.store import alias_key, brand_manufacturer, model_key, save_model

SOURCE = "energystar_index"
MODEL_INDEX = "8wj2-sec8"  # ENERGY STAR Model Index: brand, model number, category of every certified product
ES_HOSTS = {"data.energystar.gov"}
BASE_URL = "https://data.energystar.gov"
PAGE_SIZE = 10_000
PULL_EVERY = timedelta(days=30)
SEARCH_AGAIN_AFTER = timedelta(days=180)
SITE_SEARCHES_PER_RUN = 10
SITE_SEARCH_DEPTH = 2  # the results page, the product page, its PDFs
QUERY_NAMES = {"q", "s", "search", "query", "keyword", "keywords", "term", "k", "searchterm", "text"}
_WILDCARD = re.compile(r"[*?#]")
_COVERED_SQL = (  # the family has a manual: a document's model key is a 5+ character prefix of one of its models
    "EXISTS (SELECT 1 FROM public_models p JOIN document_models dm ON dm.manufacturer_id = %(maker)s "
    "AND length(dm.model_key) >= 5 AND left(p.model_key, length(dm.model_key)) = dm.model_key "
    "JOIN documents d ON d.id = dm.document_id AND d.doc_type IS NOT NULL AND d.doc_type <> 'not_equipment' "
    "AND d.hidden_at IS NULL "
    "WHERE p.brand_id = f.brand_id AND p.family = f.family)")


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fixed_key(number):
    """The model key of the fixed part of a listed model number: "GGF*****24" lists a range; its key is GGF."""
    return model_key(_WILDCARD.split(number or "")[0])


# ---------------------------------------------------------------- the public list

def pull_models(conn, fetcher, brand_id, name, *, now=None, base_url=None, allowed_hosts=None):
    """Pull the brand's rows of the Model Index unless pulled in the last PULL_EVERY. Returns a status: recent,
    pulled (with rows), failed or refused."""
    now = now or _now()
    base_url, allowed_hosts = base_url or BASE_URL, allowed_hosts or ES_HOSTS
    last = _rows(conn, "SELECT pulled_at FROM public_model_pulls WHERE brand_id = %s AND source = %s", (brand_id, SOURCE))
    if last and now - last[0][0] < PULL_EVERY:
        return {"status": "recent", "rows": 0}
    names = sorted({name.upper()} | {a.upper() for (a,) in _rows(
        conn, "SELECT a.alias FROM manufacturer_aliases a WHERE a.manufacturer_id = (SELECT manufacturer_id FROM "
              "manufacturer_aliases WHERE alias_key = %s)", (alias_key(name),))})
    where = "upper(brand_name) in (" + ", ".join("'" + n.replace("'", "''") + "'" for n in names) + ")"
    maker = brand_manufacturer(conn, brand_id)
    seen, offset, status = 0, 0, "pulled"
    while True:
        query = urlencode({"$select": "pd_id,brand_name,model_number,product_category,product_type", "$where": where,
                           "$order": "pd_id", "$limit": PAGE_SIZE, "$offset": offset})
        page = fetcher.get_json(f"{base_url.rstrip('/')}/resource/{MODEL_INDEX}.json?{query}", allowed_hosts)
        if page.outcome != "fetched":
            status = "refused" if page.outcome == "refused" else "failed"
            break
        try:
            rows = json.loads(page.body)
        except ValueError:
            status = "failed"
            break
        if not isinstance(rows, list):
            status = "failed"
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            number = str(row.get("model_number") or "").strip()[:190]
            key = fixed_key(number)[:190]
            if len(key) < 5 or not row.get("pd_id"):
                continue
            seen += 1
            _rows(conn, "INSERT INTO public_models (brand_id, source, list_id, brand_name, model_number, model_key, family, "
                        "category, product_type) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (brand_id, source, "
                        "list_id) DO UPDATE SET model_number = EXCLUDED.model_number, model_key = EXCLUDED.model_key, "
                        "family = EXCLUDED.family, category = EXCLUDED.category, product_type = EXCLUDED.product_type, "
                        "last_seen_at = (now() AT TIME ZONE 'UTC')",
                  (brand_id, SOURCE, str(row["pd_id"])[:60], str(row.get("brand_name") or "")[:190], number, key,
                   family(key), (row.get("product_category") or None), (row.get("product_type") or None)))
            save_model(conn, maker, number if not _WILDCARD.search(number) else key, energy_star=True)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    if status == "pulled":
        _rows(conn, "INSERT INTO public_model_pulls (brand_id, source, pulled_at, rows_seen, status) VALUES "
                    "(%s, %s, %s, %s, %s) ON CONFLICT (brand_id, source) DO UPDATE SET pulled_at = EXCLUDED.pulled_at, "
                    "rows_seen = EXCLUDED.rows_seen, status = EXCLUDED.status", (brand_id, SOURCE, now, seen, status))
    return {"status": status, "rows": seen}


# ---------------------------------------------------------------- coverage

def families(conn, brand_id):
    """[(family, category, covered)] of the brand's listed model families."""
    maker = brand_manufacturer(conn, brand_id)
    return _rows(conn, "SELECT f.family, f.category, " + _COVERED_SQL + " FROM (SELECT brand_id, family, "
                       "min(category) AS category FROM public_models WHERE brand_id = %(brand)s GROUP BY brand_id, family) f "
                       "ORDER BY f.family", {"maker": maker, "brand": brand_id})


def uncovered(conn, brand_id):
    return [fam for fam, _, covered in families(conn, brand_id) if not covered]


def measure(conn, brand_id, *, run_id=None, new_documents=0, gap_searches=0, site_searches=0):
    """Write and return the brand's coverage row for this run."""
    listed = families(conn, brand_id)
    by_category = {}
    for _, category, covered in listed:
        entry = by_category.setdefault(category or "other", {"families": 0, "covered": 0})
        entry["families"] += 1
        entry["covered"] += int(bool(covered))
    queued = _rows(conn, "SELECT EXISTS (SELECT 1 FROM pages WHERE brand_id = %s AND fetch_state = 'queued') OR EXISTS "
                         "(SELECT 1 FROM start_urls s WHERE s.brand_id = %s AND s.status = 'active' AND NOT EXISTS "
                         "(SELECT 1 FROM pages p WHERE p.brand_id = s.brand_id AND p.url_sha = s.url_sha))",
                   (brand_id, brand_id))[0][0]
    # families no web search has tried in its backoff (an over-budget brand made no searches but is not done)
    waiting = _rows(conn, "SELECT COUNT(*) FROM unnest(%s::text[]) f(family) WHERE NOT EXISTS (SELECT 1 FROM "
                          "gapfill_searches g WHERE g.brand_id = %s AND g.family = f.family AND g.searched_at >= "
                          "(now() AT TIME ZONE 'UTC') - %s::interval)",
                    ([f for f, _, c in listed if not c], brand_id, f"{SEARCH_AGAIN_AFTER.days} days"))[0][0]
    if not (new_documents or gap_searches or site_searches or queued or waiting):
        phase = "upkeep"
    else:
        phase = "filling_gaps" if listed else "no_public_list"
    row = {"families": len(listed), "covered": sum(1 for _, _, c in listed if c), "by_category": by_category,
           "phase": phase, "new_documents": new_documents, "gap_searches": gap_searches, "site_searches": site_searches}
    _rows(conn, "INSERT INTO coverage (brand_id, run_id, families, covered, by_category, new_documents, gap_searches, "
                "site_searches, phase) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
          (brand_id, run_id, row["families"], row["covered"], json.dumps(by_category), new_documents, gap_searches,
           site_searches, phase))
    return row


# ---------------------------------------------------------------- the site's own search

def search_template(body, page_url):
    """The site's search as an address with {q} for the words, from the page's first GET form that has a query box
    and stays on the same site; None when there is none."""
    from selectolax.lexbor import LexborHTMLParser
    try:
        tree = LexborHTMLParser(body)
    except Exception:
        return None
    page_host = (urlsplit(page_url).hostname or "").lower()
    for form in tree.css("form"):
        attrs = form.attributes
        if (attrs.get("method") or "get").strip().lower() != "get":
            continue
        action = urljoin(page_url, (attrs.get("action") or "").strip() or page_url)
        parts = urlsplit(action)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != page_host:
            continue
        query_name, fixed = None, []
        for field in form.css("input"):
            kind = (field.attributes.get("type") or "text").lower()
            name = (field.attributes.get("name") or "").strip()
            if not name:
                continue
            if kind == "hidden":
                fixed.append((name, field.attributes.get("value") or ""))
            elif kind in ("text", "search") and query_name is None and (
                    kind == "search" or name.lower() in QUERY_NAMES):
                query_name = name
        if query_name:
            base = action.split("?", 1)[0].split("#", 1)[0]
            params = urlencode(fixed + [(query_name, "QUERYWORDS")])
            return f"{base}?{params}".replace("QUERYWORDS", "{q}")
    return None


def record_search_form(conn, brand_id, page_url, body):
    """Keep the first search form found on each of the brand's hosts."""
    template = search_template(body, page_url)
    if template is None:
        return None
    host = (urlsplit(page_url).hostname or "").lower()
    _rows(conn, "INSERT INTO site_search_templates (brand_id, host, template, found_on) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (brand_id, host) DO NOTHING", (brand_id, host, template[:2000], page_url[:2000]))
    return template


def site_search(conn, brand_id, *, now=None, limit=None):
    """Search the brand's own sites for uncovered families; each search is a start address. Returns how many."""
    now, limit = now or _now(), limit if limit is not None else SITE_SEARCHES_PER_RUN
    templates = _rows(conn, "SELECT t.template FROM site_search_templates t JOIN brand_domains d ON d.brand_id = t.brand_id "
                            "AND d.host = t.host AND d.status = 'approved' AND d.scope = 'all' WHERE t.brand_id = %s "
                            "ORDER BY t.host", (brand_id,))
    if not templates:
        return 0
    done = 0
    for fam in uncovered(conn, brand_id):
        if done >= limit:
            break
        last = _rows(conn, "SELECT searched_at FROM site_searches WHERE brand_id = %s AND family = %s", (brand_id, fam))
        if last and now - last[0][0] < SEARCH_AGAIN_AFTER:
            continue
        url = templates[0][0].replace("{q}", quote(fam))
        _rows(conn, "INSERT INTO start_urls (brand_id, url, url_sha, found_by, depth) VALUES (%s, %s, %s, 'gapfill', %s) "
                    "ON CONFLICT (brand_id, url_sha) DO NOTHING",
              (brand_id, url, hashlib.sha256(url.encode("utf-8")).hexdigest(), SITE_SEARCH_DEPTH))
        _rows(conn, "INSERT INTO site_searches (brand_id, family, searched_at, url) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (brand_id, family) DO UPDATE SET searched_at = EXCLUDED.searched_at, url = EXCLUDED.url",
              (brand_id, fam, now, url))
        done += 1
    return done
