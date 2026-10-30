"""Gap-fill: a targeted web search for ENERGY STAR models that still have no document (D15).

One search per model family, limited to the brand's approved hosts, after a free check of the text we
already hold. At most 200 searches a month; a search that finds nothing is not repeated for 6 months.
Nothing here commits unless the caller does.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from crawler import ai
from crawler.brands import UnknownTarget, find_brand
from crawler.db import try_lock, unlock
from crawler.discover import _clean_url
from crawler.hosts import in_scope
from crawler.store import HAS_NO_DOCUMENT_SQL, alias_key

import hashlib

MAX_SEARCHES_PER_MONTH = 200
MAX_TOKENS = 4000
ASSUMED_INPUT_TOKENS = 60_000
MISS_BACKOFF = timedelta(days=180)
DEFAULT_CAP_USD = "25"

GAP_SYSTEM = (
    "You look for official documents (spec sheets, installation manuals, data sheets) for one equipment model family "
    "of one brand. Use web search, limited to the sites you are allowed. Report only documents you actually saw "
    "in the results, each with the page where you saw it (evidence_url). If you saw none, answer with an empty list. "
    "Answer with JSON only."
)
GAP_SCHEMA = {
    "type": "object",
    "properties": {"documents": {"type": "array", "items": {
        "type": "object", "properties": {"url": {"type": "string"}, "evidence_url": {"type": "string"}},
        "required": ["url", "evidence_url"], "additionalProperties": False}}},
    "required": ["documents"], "additionalProperties": False,
}


def family(model_key):
    return model_key[:6]


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)


def _holds_text(conn, fam):
    """True when stored page text mentions the family, with or without stray spaces or hyphens in it."""
    pattern = r"[\s-]*".join(re.escape(c) for c in fam)
    return bool(_rows(conn, "SELECT 1 FROM page_text WHERE body ~ %s LIMIT 1", (pattern,)))


def _month_bounds(now):
    start = datetime(now.year, now.month, 1)
    return start, datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)


def gap_fill(conn, client, **kwargs):
    if not try_lock(conn, "crawler-gapfill"):
        raise RuntimeError("Another gapfill is already running.")
    try:
        return _gap_fill(conn, client, **kwargs)
    finally:
        unlock(conn, "crawler-gapfill")


def _gap_fill(conn, client, *, brand=None, now=None, cap_usd=None, max_searches=MAX_SEARCHES_PER_MONTH, after_search=None,
              limit=None):
    """limit: at most this many searches in this call (the nightly run's share, P20); the monthly cap holds too."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cap = Decimal(str(cap_usd if cap_usd is not None else os.environ.get("AI_MONTHLY_CAP_USD", DEFAULT_CAP_USD)))
    summary = {"searched": 0, "found": 0, "held_text": 0, "backed_off": 0}

    if brand is not None:
        brand_id = find_brand(conn, brand)
        if brand_id is None:
            raise UnknownTarget(f"No brand called {brand}.")
        brands = _rows(conn, "SELECT id, name FROM brands WHERE id = %s AND status = 'active'", (brand_id,))
    else:
        brands = _rows(conn, "SELECT id, name FROM brands WHERE status = 'active' ORDER BY id")

    start, end = _month_bounds(now)
    used = int(_rows(conn, "SELECT COALESCE(SUM(searches), 0) FROM ai_calls WHERE purpose = 'gapfill' "
                           "AND created_at >= %s AND created_at < %s", (start, end))[0][0])
    model = None

    for brand_id, brand_name in brands:
        hosts = tuple(sorted(r[0] for r in _rows(
            conn, "SELECT host FROM brand_domains WHERE brand_id = %s AND status = 'approved'", (brand_id,))))
        if not hosts:
            continue
        keys = [r[0] for r in _rows(
            conn,
            "SELECT em.model_key FROM models em WHERE em.energy_star_confirmed AND em.manufacturer_id IN "
            "(SELECT id FROM manufacturers WHERE name_key = %s UNION "
            " SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s) "
            "AND " + HAS_NO_DOCUMENT_SQL,
            (alias_key(brand_name), alias_key(brand_name)))]
        for fam in sorted({family(k) for k in keys}):
            if used >= max_searches or (limit is not None and summary["searched"] >= limit):
                return summary
            last = _rows(conn, "SELECT searched_at, found FROM gapfill_searches WHERE brand_id = %s AND family = %s",
                         (brand_id, fam))
            if last and now - last[0][0] < MISS_BACKOFF:
                summary["backed_off"] += 1
                continue
            if _holds_text(conn, fam):
                summary["held_text"] += 1
                continue
            if model is None:
                model = ai.HAIKU if client.supports_web_search(ai.HAIKU) else ai.SONNET
            done = _search(conn, client, model, brand_id, brand_name, fam, hosts, cap, now)
            if done is None:
                return summary
            used += done[1]
            summary["searched"] += 1
            summary["found"] += 1 if done[0] else 0
            if after_search:
                after_search()
    return summary


def search_family(conn, client, brand_id, fam, *, now=None, cap_usd=None, purpose="gapfill"):
    """One search for one family of the brand (a Breeze miss on a model with no manual, P21). Returns how many
    documents it queued, or None when it could not search (no approved host, the monthly cap)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cap = Decimal(str(cap_usd if cap_usd is not None else os.environ.get("AI_MONTHLY_CAP_USD", DEFAULT_CAP_USD)))
    name = _rows(conn, "SELECT name FROM brands WHERE id = %s", (brand_id,))[0][0]
    hosts = tuple(sorted(r[0] for r in _rows(
        conn, "SELECT host FROM brand_domains WHERE brand_id = %s AND status = 'approved'", (brand_id,))))
    if not hosts:
        return None
    model = ai.HAIKU if client.supports_web_search(ai.HAIKU) else ai.SONNET
    done = _search(conn, client, model, brand_id, name, fam, hosts, cap, now, purpose)
    return None if done is None else done[0]


def _search(conn, client, model, brand_id, brand_name, fam, hosts, cap, now, purpose="gapfill"):
    """(documents queued, searches used) for one family, or None at the monthly cap."""
    worst = ai.cost(model, input_tokens=ASSUMED_INPUT_TOKENS, output_tokens=MAX_TOKENS, searches=1)
    reserved = Decimal(_rows(conn, "SELECT COALESCE(SUM(ai_reserved_usd), 0) FROM pages "
                             "WHERE ai_batch_id IS NOT NULL")[0][0])
    if ai.monthly_spend(conn, now) + reserved + worst > cap:
        return None
    query = f"{brand_name} {fam} spec sheet or installation manual"
    reply = client.find_documents(model, brand_name, query, hosts, 1, MAX_TOKENS)
    answer = reply.output if isinstance(reply.output, dict) else None
    _exec(conn,
          "INSERT INTO ai_calls (purpose, brand_id, page_id, model, input_tokens, output_tokens, cache_read_tokens, "
          "cache_write_tokens, searches, is_batch, rejected, cost_usd, created_at) "
          "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, false, %s, %s, %s)",
          (purpose, brand_id, model, reply.input_tokens, reply.output_tokens, reply.cache_read_tokens,
           reply.cache_write_tokens, reply.searches, answer is None,
           ai.cost(model, reply.input_tokens, reply.output_tokens, searches=reply.searches), now))
    stored = 0
    for entry in (answer or {}).get("documents", []) if isinstance(answer, dict) and isinstance(
            answer.get("documents"), list) else []:
        url = _clean_url(entry.get("url")) if isinstance(entry, dict) else None
        if url is None or not in_scope(url, list(hosts)):
            continue
        _exec(conn, "INSERT INTO start_urls (brand_id, url, url_sha, found_by) VALUES (%s, %s, %s, 'gapfill') "
                    "ON CONFLICT (brand_id, url_sha) DO NOTHING", (brand_id, url, hashlib.sha256(url.encode("utf-8")).hexdigest()))
        stored += 1
    _exec(conn, "INSERT INTO gapfill_searches (brand_id, family, searched_at, found) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (brand_id, family) DO UPDATE SET searched_at = EXCLUDED.searched_at, found = EXCLUDED.found",
          (brand_id, fam, now, bool(stored)))
    return stored, max(reply.searches, 1)
