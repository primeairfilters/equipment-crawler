"""Brand discovery: one live web-search call per brand, every 180 days. Found hosts wait for `ok`."""
import ipaddress
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit
import hashlib
import os

from crawler import ai
from crawler.brands import UnknownTarget, find_brand
from crawler.hosts import is_refused_host, normalize_host

MAX_USES = 8
MAX_TOKENS = 8000
ASSUMED_INPUT_TOKENS = 150_000  # worst case for the cap: search results are read as input, over the 100k price step
REDO_AFTER = timedelta(days=180)
DEFAULT_CAP_USD = "25"
MAX_URL_LENGTH = 2048

DISCOVER_SYSTEM = (
    "You find where a heating, cooling or refrigeration equipment brand publishes its product data online.\n"
    "Use web search. Report only what you actually saw in search results:\n"
    "- domains: the brand's own web domains (host names only, like www.ruud.com).\n"
    "- document_hosts: other hosts that serve the brand's spec sheets, manuals or PDFs (like files.myrheem.com).\n"
    "- start_urls: listing pages on the brand's own site (one of the domains above), one per product family: the page that "
    "lists that family's products. Also include the brand's document library or literature page (where its manuals and "
    "spec sheets are listed), if it has one.\n"
    "Never use third-party manual sites (like manualslib.com) for any of these.\n"
    "Every entry needs evidence_url: the page where you saw it. Do not guess. Leave out anything you did not see.\n"
    "Never include ahridirectory.org or ahrinet.org. Answer with JSON only."
)
_ENTRY = {"type": "object", "properties": {"host": {"type": "string"}, "evidence_url": {"type": "string"}},
          "required": ["host", "evidence_url"], "additionalProperties": False}
DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "domains": {"type": "array", "items": _ENTRY},
        "document_hosts": {"type": "array", "items": _ENTRY},
        "start_urls": {"type": "array", "items": {
            "type": "object", "properties": {"url": {"type": "string"}, "evidence_url": {"type": "string"}},
            "required": ["url", "evidence_url"], "additionalProperties": False}},
    },
    "required": ["domains", "document_hosts", "start_urls"], "additionalProperties": False,
}


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)


def _clean_host(value):
    """A normalised public-looking host name, or None. Never an address, a single word, or an AHRI host."""
    if not isinstance(value, str):
        return None
    try:
        host = normalize_host(value)
    except ValueError:
        return None
    if "." not in host or is_refused_host(host):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    return host


def _clean_url(value):
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in ("http", "https") or _clean_host(parts.hostname or "") is None:
            return None
    except ValueError:
        return None
    return value.strip()


def _evidence(value):
    return _clean_url(value)


def _items(answer, key):
    value = answer.get(key)
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def discover(conn, client, brand_name, *, now=None, cap_usd=None):
    """Find hosts and start URLs for a brand. Does nothing if it was done in the last 180 days or the cap is short."""
    now = now or _utcnow()
    brand_id = find_brand(conn, brand_name)
    if brand_id is None:
        raise UnknownTarget(f"No brand called {brand_name}.")
    done_at = _rows(conn, "SELECT discovered_at FROM brands WHERE id = %s", (brand_id,))[0][0]
    if done_at is not None and now - done_at < REDO_AFTER:
        return "recent"

    model = ai.HAIKU if client.supports_web_search(ai.HAIKU) else ai.SONNET
    cap = Decimal(str(cap_usd if cap_usd is not None else os.environ.get("AI_MONTHLY_CAP_USD", DEFAULT_CAP_USD)))
    worst = ai.cost(model, input_tokens=ASSUMED_INPUT_TOKENS, output_tokens=MAX_TOKENS, searches=MAX_USES, check_long=True)
    reserved = Decimal(_rows(conn, "SELECT COALESCE(SUM(ai_reserved_usd), 0) FROM pages WHERE ai_batch_id IS NOT NULL")[0][0])
    if ai.monthly_spend(conn, now) + reserved + worst > cap:
        return "budget"

    reply = client.discover(model, brand_name, MAX_USES, MAX_TOKENS)
    answer = reply.output if isinstance(reply.output, dict) else None
    _exec(conn,
          "INSERT INTO ai_calls (purpose, brand_id, page_id, model, input_tokens, output_tokens, cache_read_tokens, "
          "cache_write_tokens, searches, is_batch, rejected, cost_usd, created_at) "
          "VALUES ('discover', %s, NULL, %s, %s, %s, %s, %s, %s, false, %s, %s, %s)",
          (brand_id, model, reply.input_tokens, reply.output_tokens, reply.cache_read_tokens, reply.cache_write_tokens,
           reply.searches, answer is None,
           ai.cost(model, reply.input_tokens, reply.output_tokens, searches=reply.searches, check_long=True), now))
    if answer is None:
        return "unusable"

    own = set()
    for key in ("domains", "document_hosts"):
        for entry in _items(answer, key):
            host = _clean_host(entry.get("host"))
            if host is None:
                continue
            if key == "domains":
                own.add(host)
            _exec(conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, evidence_url) "
                        "VALUES (%s, %s, 'waiting', 'ai', %s) ON CONFLICT (brand_id, host) DO NOTHING",
                  (brand_id, host, _evidence(entry.get("evidence_url"))))
    for entry in _items(answer, "start_urls"):
        url = _clean_url(entry.get("url"))
        if url is None:
            continue
        host = _clean_host(urlsplit(url).hostname or "")  # only the brand's own domains (or their subdomains) are kept
        if not any(host == d or host.endswith("." + d) for d in own):
            continue
        _exec(conn, "INSERT INTO start_urls (brand_id, url, url_sha, found_by) VALUES (%s, %s, %s, 'discover') "
                    "ON CONFLICT (brand_id, url_sha) DO NOTHING", (brand_id, url, hashlib.sha256(url.encode("utf-8")).hexdigest()))
    _exec(conn, "UPDATE brands SET discovered_at = %s WHERE id = %s", (now, brand_id))
    return answer
