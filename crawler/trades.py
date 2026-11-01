"""Tracforce's one trades list (user 2026-11-01): the main app's GET /api/public/trades, read from the file its signup
screen uses. `./crawl add-brands` checks a list's trades against it. "Other" is a signup answer, not a trade a brand
can have, so it is left out. TRACFORCE_TRADES_URL points somewhere else (the tests, a local main app)."""
import json
import os
import urllib.request

DEFAULT_URL = "https://app.tracforce.ai/api/public/trades"
NOT_A_BRAND_TRADE = "other"


class Unavailable(RuntimeError):
    pass


def fetch(url=None, opener=urllib.request.urlopen):
    """The trade keys in Tracforce's order, without Other. Raises Unavailable when the list cannot be read."""
    url = url or os.environ.get("TRACFORCE_TRADES_URL") or DEFAULT_URL
    try:
        with opener(url, timeout=10) as response:
            trades = json.load(response)["trades"]
        keys = [t["value"] for t in trades if t["value"] != NOT_A_BRAND_TRADE]
    except Exception as error:
        raise Unavailable(f"Could not read Tracforce's trades list from {url}: {error}") from error
    if not keys:
        raise Unavailable(f"Tracforce's trades list at {url} is empty")
    return keys
