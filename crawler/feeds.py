"""Document feeds (D68b): the JSON APIs that JavaScript document libraries call, walked directly.

Cheaper and gentler than rendering the page. Inside a feed response, every string that is an address is a link:
PDFs become documents (the entry's title, name or label is the link text), and addresses under the feed's own API
folder are fetched as more JSON. Some feeds name categories by slug instead of by address; FEED_RULES turns a slug
into its category request for the hosts known to do that.
"""
import json
from urllib.parse import quote, urljoin, urlsplit

TEXT_KEYS = ("title", "name", "label", "displayName", "display_name", "description")

# host (or a parent domain) -> {key holding a slug: the request it means, relative to the response's address}
FEED_RULES = {
    "mylinkdrive.com": {"slugName": "category?slugName={}"},
}


def feed_prefix(start_url):
    """The API folder a feed lives in: /api/v0/categoryMenu?x -> /api/v0/."""
    path = urlsplit(start_url).path or "/"
    return path[: path.rfind("/") + 1]


def _slug_rules(host):
    for domain, rules in FEED_RULES.items():
        if host == domain or host.endswith("." + domain):
            return rules
    return {}


def _looks_like_address(value):
    return value.startswith(("http://", "https://")) or (value.startswith("/") and not value.startswith("//"))


def feed_links(body, base_url):
    """(address, text) for every address in a JSON feed response, in the order found. Anything unreadable gives []."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return []
    rules = _slug_rules((urlsplit(base_url).hostname or "").lower())
    found = []

    def walk(node, text):
        if isinstance(node, dict):
            own = next((node[k] for k in TEXT_KEYS if isinstance(node.get(k), str) and node[k].strip()), None)
            text = own or text
            for key, value in node.items():
                if isinstance(value, str):
                    value = value.strip()
                    if key in rules and value:
                        found.append((urljoin(base_url, rules[key].format(quote(value, safe=""))), text))
                    elif _looks_like_address(value) and len(value) <= 2000:
                        found.append((urljoin(base_url, value), text))
                else:
                    walk(value, text)
        elif isinstance(node, list):
            for item in node:
                walk(item, text)

    walk(data, None)
    return [(url, " ".join((text or "").split())[:500]) for url, text in found]
