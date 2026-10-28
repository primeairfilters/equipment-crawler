"""The free rules that decide a link before any AI is asked (D61, D62).

Keywords match whole parts of an address, never "contains": a whole subdomain label (blog.ruud.com -> blog) or a
whole path folder (/blog/). A trailing * covers variants (news* -> news, newsroom, news-events). PDFs are always
crawled. A learned rule covers one host's first folder. Otherwise signals in the address and the link text decide,
and a tie is "unsure", which goes to the AI.
"""
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

KEYWORD = re.compile(r"^[a-z0-9][a-z0-9_-]*\*?$")
LEARN_AFTER = 5  # matching AI answers for one host's folder before a rule decides it for free

_MODEL_CODE = re.compile(r"(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9]{2,}(?:-?[a-z0-9]+)*")
_GOOD_TEXT = re.compile(
    r"\b(spec(ification)?s?|spec sheets?|data ?sheets?|manuals?|literature|documents?|documentation|downloads?|"
    r"install(ation)?|service|parts|warranty|brochures?|products?|troubleshooting|wiring|technical)\b", re.I)
_BAD_TEXT = re.compile(
    r"\b(read more|press releases?|news|blog|careers?|jobs|privacy|terms|cookies?|log ?in|sign ?in|cart|"
    r"subscribe|share)\b", re.I)
_GOOD_FOLDERS = {"product", "products", "literature", "documents", "document", "downloads", "download", "manuals",
                 "specs", "specifications", "resources", "support", "technical", "docs"}


@dataclass(frozen=True)
class Keyword:
    keyword: str
    effect: str  # skip | prefer
    host: str = None  # set on a learned folder rule: it covers this host only
    source: str = "user"


@dataclass(frozen=True)
class Decision:
    verdict: str  # crawl | skip | unsure
    reason: str = None
    by: str = None  # rule | ai | user, None while unsure
    state: str = None  # the fetch_state for a skip

    @property
    def preferred(self):
        """A link the rules want (a prefer keyword, a learned rule or a good signal) goes before the rest."""
        return self.verdict == "crawl" and self.by == "rule"


def is_valid_keyword(word):
    return bool(KEYWORD.match(word or "")) and len(word) <= 60


def _host(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def subdomain_labels(host):
    """The labels left of the site's own name: blog.ruud.com -> [blog]; www.ruud.com -> [www]; ruud.com -> []."""
    labels = host.split(".")
    return labels[:-2] if len(labels) > 2 else []


def path_segments(url):
    try:
        path = urlsplit(url).path
    except ValueError:
        return []
    return [unquote(s).lower() for s in path.split("/") if s]


def folder_of(url):
    segments = path_segments(url)
    return segments[0] if segments else None


def parent_folder(url):
    """The folder a page sits in, all its segments: /en/about/team -> en/about. None for a page at the root."""
    segments = path_segments(url)
    return "/".join(segments[:-1]) if len(segments) >= 2 else None


def is_pdf_link(url):
    try:
        return urlsplit(url).path.lower().endswith(".pdf")
    except ValueError:
        return False


def _word_hits(keyword, part):
    keyword = keyword.lower()
    if keyword.endswith("*"):
        return part.startswith(keyword[:-1])
    return part == keyword


def keyword_hits(keyword, url):
    parts = subdomain_labels(_host(url)) + path_segments(url)
    return any(_word_hits(keyword, part) for part in parts)


def _learned_hit(rule, url):
    """A learned rule covers its folder and the folders below it, on its own host."""
    parent = parent_folder(url)
    folder = rule.keyword.lower()
    return rule.host == _host(url) and parent is not None and (parent == folder or parent.startswith(folder + "/"))


def decide(url, text, *, keywords, page_is_article=False):
    text = " ".join((text or "").split())
    if is_pdf_link(url):
        return Decision("crawl", "a PDF", "rule")
    plain = [k for k in keywords if k.host is None]
    for k in plain:
        if k.effect == "skip" and keyword_hits(k.keyword, url):
            return Decision("skip", f"skip keyword '{k.keyword}'", "rule", "skipped_keyword")
    learned_skip = None
    for k in keywords:
        if k.host is not None and _learned_hit(k, url):
            if k.effect == "skip":
                learned_skip = learned_skip or k
            else:
                return Decision("crawl", f"learned rule {k.host}/{k.keyword}/", "rule")
    for k in plain:
        if k.effect == "prefer" and keyword_hits(k.keyword, url):
            return Decision("crawl", f"prefer keyword '{k.keyword}'", "rule")

    good, bad = [], []
    segments = path_segments(url)
    if any(len(m) >= 5 for s in segments for m in _MODEL_CODE.findall(s)):
        good.append("a model-like code in the address")
    if any(s in _GOOD_FOLDERS for s in segments):
        good.append("a product or document folder")
    if _GOOD_TEXT.search(text):
        good.append(f"link text '{text[:60]}'")
    if _BAD_TEXT.search(text):
        bad.append(f"link text '{text[:60]}'")
    if page_is_article:
        bad.append("the link is on an article page")
    if len(good) > len(bad):
        return Decision("crawl", "; ".join(good), "rule")
    if learned_skip is not None:  # a learned skip never beats a good signal in the address or text
        return Decision("skip", f"learned rule {learned_skip.host}/{learned_skip.keyword}/", "rule", "skipped_rule")
    if len(bad) > len(good):
        return Decision("skip", "; ".join(bad), "rule", "skipped_rule")
    return Decision("unsure")


def load_keywords(conn, brand_id):
    """Active keywords for this brand and for every brand, learned folder rules included."""
    with conn.cursor() as cur:
        cur.execute("SELECT keyword, effect, host, source FROM crawl_keywords WHERE active "
                    "AND (brand_id IS NULL OR brand_id = %s) ORDER BY id", (brand_id,))
        return [Keyword(*row) for row in cur.fetchall()]
