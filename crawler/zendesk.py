"""Help centers (Zendesk): an article page is read through the site's own help-center API.

Some help centers turn the crawler away on their article pages but answer on the help-center API, which their
robots.txt also allows (support.trane.com, 2026-11-21: 403 on all 2,494 article pages, 200 on the API). An address
like /hc/en-us/articles/123-title is asked for as /api/v2/help_center/en-us/articles/123.json, and the article is kept
as a small web page (its title and its body) under its own address. The fetcher falls back to the page when the API
does not answer with one article.
"""
import json
import re
from html import escape
from urllib.parse import urlsplit, urlunsplit

_ARTICLE = re.compile(r"^/hc/([a-z]{2}(?:-[a-z0-9]{2,4})?)/articles/(\d+)(?:-[^/]*)?/?$", re.IGNORECASE)


def article_api(url):
    """The help-center API address for a help-center article page, else None."""
    parts = urlsplit(url)
    found = _ARTICLE.match(parts.path)
    if not found or parts.path.lower().endswith(".pdf"):
        return None
    return urlunsplit((parts.scheme, parts.netloc,
                       f"/api/v2/help_center/{found.group(1).lower()}/articles/{found.group(2)}.json", "", ""))


def article_html(body):
    """The API's answer as a small web page (title and body), or None when it is not one article."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    article = data.get("article") if isinstance(data, dict) else None
    if not isinstance(article, dict) or not isinstance(article.get("body"), str):
        return None
    title = escape(str(article.get("title") or ""))
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{article['body']}</body></html>".encode()
