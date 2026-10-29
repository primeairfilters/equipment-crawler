"""run: crawl each active brand's start addresses, inside the limits, and record every link and why it was or
wasn't fetched (D57-D62).

Per start address: its own depth (default 3) and page limit. Pages are taken shallowest first, so each is walked at
its smallest depth; within a depth, PDFs go first, then links the rules like, then the rest.
Every link is kept in page_links with its text. A link past the depth or the limit, or skipped by a keyword, a rule,
the AI or the user, is recorded with that reason and never requested. Links the rules are unsure about go to the AI
link check, one call per page; with no AI (no key, or the cap reached) they wait as ask_ai. Every original is kept in
the library, and every PDF becomes a document. A feed start address is a JSON document API, walked without a browser.
"""
import hashlib
import heapq
import re
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from crawler import library
from crawler.brands import UnknownTarget, find_brand
from crawler.documents import record_pdf
from crawler.feeds import feed_links, feed_prefix
from crawler.hosts import in_scope, normalize_host
from crawler.rules import (LEARN_AFTER, Decision, Keyword, decide, is_pdf_link, is_valid_keyword, load_keywords,
                           parent_folder)
from crawler.textextract import index_page

PDF_FIRST, PREFERRED, OTHER = 0, 1, 2
_ARTICLE_LD = re.compile(r'"@type"\s*:\s*"(NewsArticle|BlogPosting|Article)"')


def extract_links(body, base_url):
    """(absolute http(s) link, link text) for each link in an HTML page, fragments removed, in page order."""
    links = []
    for node in LexborHTMLParser(body).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href:
            continue
        try:
            url = urldefrag(urljoin(base_url, href))[0]
            if urlsplit(url).scheme not in ("http", "https"):
                continue
        except ValueError:
            continue
        text = node.text(deep=True, separator=" ", strip=True) or node.attributes.get("title") or \
            node.attributes.get("aria-label") or ""
        links.append((url, " ".join(text.split())[:500]))
    return links


def page_context(body, url):
    """What the AI sees about the page a link is on: its address, title and first heading, and whether it is an article."""
    tree = LexborHTMLParser(body)
    title = tree.css_first("title")
    heading = tree.css_first("h1")
    og = tree.css_first('meta[property="og:type"]')
    article = bool(og is not None and (og.attributes.get("content") or "").lower() == "article")
    for script in tree.css('script[type="application/ld+json"]'):
        if _ARTICLE_LD.search(script.text() or ""):
            article = True
    return {"url": url, "title": title.text(strip=True)[:300] if title is not None else "",
            "heading": heading.text(strip=True)[:300] if heading is not None else "", "article": article}


def _seen_key(url):
    """/x and /x/ are one page, and so are a path written plain and percent-encoded ("/A B®" and "/A%20B%C2%AE"); the
    root stays as it is."""
    parts = urlsplit(url)
    path = unquote(parts.path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit(parts._replace(path=path))


def _spellings(url):
    """The ways one page's address may have been stored: as given, plain, percent-encoded, each with and without the
    trailing slash (Ruud links the same product page both ways)."""
    parts = urlsplit(url)
    plain = unquote(parts.path)
    forms = [url] + [urlunsplit(parts._replace(path=path))
                     for path in (plain, quote(plain, safe="/:@!$&'()*+,;=-._~"))]
    out = []
    for form in forms:
        out += [form, _other_slash(form)]
    return [f for f in dict.fromkeys(out) if f is not None]


def _other_slash(url):
    parts = urlsplit(url)
    if len(parts.path) > 1 and parts.path.endswith("/"):
        return urlunsplit(parts._replace(path=parts.path[:-1]))
    if parts.path and parts.path != "/":
        return urlunsplit(parts._replace(path=parts.path + "/"))
    return None


def _sha(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def run(conn, fetcher, *, brand=None, max_pages=2000, after_page=None, link_checker=None):
    summary = {"fetched": 0, "not_modified": 0, "failed": 0, "refused": 0}
    if brand is not None:
        brand_id = find_brand(conn, brand)
        if brand_id is None:
            raise UnknownTarget(f"No brand called {brand}.")
        brands = _rows(conn, "SELECT id FROM brands WHERE id = %s AND status = 'active'", (brand_id,))
    else:
        brands = _rows(conn, "SELECT id FROM brands WHERE status = 'active' ORDER BY id")

    for (brand_id,) in brands:
        keywords = load_keywords(conn, brand_id)
        starts = _rows(conn, "SELECT id, url, depth, kind FROM start_urls WHERE brand_id = %s AND status = 'active' "
                             "ORDER BY id", (brand_id,))
        for start_id, url, depth, kind in starts:
            if not _active(conn, brand_id, start_id):
                break  # stopped during the run
            # each start walks its own tree; a page another start already read this run is not due, so costs no request
            _StartCrawl(conn, fetcher, brand_id, start_id, url, depth, kind, keywords, set(), link_checker, max_pages,
                        after_page, summary).go()
    return summary


def _active(conn, brand_id, start_id):
    """False once `./crawl stop` has stopped the brand or this start address (a run checks before each page)."""
    return bool(_rows(conn, "SELECT 1 FROM brands b JOIN start_urls s ON s.brand_id = b.id WHERE b.id = %s AND s.id = %s "
                            "AND b.status = 'active' AND s.status = 'active'", (brand_id, start_id)))


class _StartCrawl:
    def __init__(self, conn, fetcher, brand_id, start_id, start_url, depth, kind, keywords, seen, link_checker,
                 max_pages, after_page, summary):
        self.conn, self.fetcher, self.brand_id, self.start_id = conn, fetcher, brand_id, start_id
        self.start_url, self.max_depth, self.kind = urldefrag(start_url)[0], depth, kind
        self.keywords, self.seen, self.link_checker = keywords, seen, link_checker
        self.max_pages, self.after_page, self.summary = max_pages, after_page, summary
        self.prefix = feed_prefix(self.start_url) if kind == "feed" else None
        self.approved = fetcher._approved(brand_id)
        self.heap, self.order = [], 0

    # ------------------------------------------------------------ the frontier

    def push(self, url, depth, parent_id, kind, priority):
        self.order += 1
        heapq.heappush(self.heap, (depth, priority, self.order, url, depth, parent_id, kind))

    def go(self):
        started = self.fetcher.now()
        run_id = _rows(self.conn, "INSERT INTO crawl_runs (started_at, brand_id, start_url_id) VALUES (%s, %s, %s) "
                                  "RETURNING id", (started, self.brand_id, self.start_id))[0][0]
        self.push(self.start_url, 0, None, "json" if self.kind == "feed" else "page", PDF_FIRST)
        fetched, limit_hit, left = 0, False, 0
        while self.heap:
            _, priority, _, url, depth, parent_id, kind = heapq.heappop(self.heap)
            if _seen_key(url) in self.seen:
                continue
            if fetched >= self.max_pages:
                limit_hit = True
                left = self._over_limit([url] + [item[3] for item in self.heap])
                break
            if not _active(self.conn, self.brand_id, self.start_id):
                break
            if self._newly_approved():
                self.push(url, depth, parent_id, kind, priority)  # judged again after the PDFs that came into scope
                continue
            self.seen.add(_seen_key(url))
            fetched += self._visit(url, depth, parent_id, kind)
        _rows(self.conn, "UPDATE crawl_runs SET finished_at = %s, pages_fetched = %s, limit_hit = %s, links_left = %s "
                         "WHERE id = %s AND started_at = %s", (self.fetcher.now(), fetched, limit_hit, left, run_id, started))

    def _newly_approved(self):
        """A host approved since the run started reaches it: its PDF links recorded as out of scope are queued (within
        the depth limit). True when any were queued."""
        approved = self.fetcher._approved(self.brand_id)
        if set(approved) <= set(self.approved):
            return False
        self.approved, queued = approved, False
        for row_id, url, depth, parent_id in _rows(
                self.conn, "SELECT id, url, depth, parent_id FROM pages WHERE brand_id = %s AND fetch_state = 'out_of_scope' "
                           "ORDER BY id", (self.brand_id,)):
            if in_scope(url, approved) and (depth or 0) <= self.max_depth and _seen_key(url) not in self.seen:
                _rows(self.conn, "UPDATE pages SET fetch_state = 'queued' WHERE id = %s", (row_id,))
                self.push(url, depth or 0, parent_id, "page", PDF_FIRST)
                queued = True
        return queued

    def _over_limit(self, urls):
        left = set()
        for url in urls:
            if _seen_key(url) in self.seen or _seen_key(url) in left:
                continue
            left.add(_seen_key(url))
            _rows(self.conn, "UPDATE pages SET fetch_state = 'over_limit' WHERE brand_id = %s AND url_sha = %s "
                             "AND fetched_at IS NULL", (self.brand_id, _sha(url)))
        return len(left)

    # ------------------------------------------------------------ one page

    def _visit(self, url, depth, parent_id, kind):
        """Fetch (if due) and walk one page. Returns 1 when a request was attempted, else 0."""
        due, page_id = self.fetcher.due_info(self.brand_id, url)
        links, context, attempted = [], {"url": url, "title": "", "heading": "", "article": False}, 0
        if not due:
            links = self._known_children(page_id)
        else:
            result = self.fetcher.fetch(url, self.brand_id, depth=depth, start_url_id=self.start_id, parent_id=parent_id,
                                        kind=kind)
            self.summary[result.outcome] += 1
            attempted = 0 if result.outcome == "refused" else 1
            page_id = result.page_id
            if result.url and _seen_key(result.url) != _seen_key(url):
                self.seen.add(_seen_key(result.url))  # a redirect's target is not fetched again this run
            if result.outcome == "refused":
                _rows(self.conn, "UPDATE pages SET fetch_state = 'refused' WHERE brand_id = %s AND url_sha = %s "
                                 "AND fetched_at IS NULL", (self.brand_id, _sha(url)))
            if result.body and result.mime:
                library.keep(self.conn, result.body, result.mime)
            if result.outcome == "fetched":
                if kind == "json":
                    _rows(self.conn, "UPDATE pages SET extraction_status = 'skipped', page_count = 0 WHERE id = %s",
                          (page_id,))
                    links = feed_links(result.body, result.url)
                else:
                    index_page(self.conn, page_id, result.body, is_pdf=result.is_pdf)
                    if result.is_pdf:
                        record_pdf(self.conn, self.brand_id, page_id, url, result.body)
                    else:
                        links = extract_links(result.body, result.url)
                        context = page_context(result.body, result.url)
            elif result.outcome == "not_modified":
                if result.is_pdf and result.body:
                    record_pdf(self.conn, self.brand_id, page_id, url, result.body)
                links = self._known_children(page_id)
        if page_id is not None and links:
            if not context["title"]:
                title = _rows(self.conn, "SELECT title FROM page_text WHERE page_id = %s AND page_number = 1", (page_id,))
                context["title"] = (title[0][0] or "") if title else ""
            self._children(page_id, depth + 1, links, context, kind)
        if due and self.after_page is not None:
            self.after_page()  # once per page read, after its links are recorded, so a stop mid-run keeps them
        return attempted

    def _known_children(self, page_id):
        """The links already recorded on this page, so a page that is not read again still leads to its children."""
        if page_id is None:
            return []
        return [tuple(r) for r in _rows(
            self.conn, "SELECT t.url, l.link_text FROM page_links l JOIN pages t ON t.id = l.to_page_id "
                       "WHERE l.from_page_id = %s ORDER BY t.id", (page_id,))]

    # ------------------------------------------------------------ links

    def _row(self, url, depth, parent_id, state="queued"):
        """The page row for a link, made if new. Its slash twin and its plain or encoded spelling count as the same page
        (_spellings); of several stored, the one already fetched."""
        found = _rows(self.conn, "SELECT id, fetch_state, decision, decided_by, fetched_at IS NOT NULL, url FROM pages "
                                 "WHERE brand_id = %s AND url_sha = ANY (%s) ORDER BY fetched_at IS NULL, id",
                      (self.brand_id, [_sha(c) for c in _spellings(url)]))
        if found:
            return found[0]
        host_id = self.fetcher._host_row(normalize_host(urlsplit(url).hostname or ""))["id"]
        return _rows(self.conn,
                     "INSERT INTO pages (brand_id, host_id, start_url_id, parent_id, url, url_sha, depth, fetch_state) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (brand_id, url_sha) DO UPDATE SET url = pages.url "
                     "RETURNING id, fetch_state, decision, decided_by, fetched_at IS NOT NULL, url",
                     (self.brand_id, host_id, self.start_id, parent_id, url, _sha(url), depth, state))[0]

    def _link(self, from_id, to_id, text):
        _rows(self.conn, "INSERT INTO page_links (from_page_id, to_page_id, link_text, first_seen_at) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT (from_page_id, to_page_id) DO NOTHING",
              (from_id, to_id, text or None, self.fetcher.now()))

    def _set(self, row_id, fetched, decision, state=None):
        """Store a decision. A page fetched before keeps fetch_state 'fetched'; the decision still rules future runs. A
        failed page still to be crawled stays 'failed' (its retry wait is in next_check_at), so status counts it."""
        _rows(self.conn, "UPDATE pages SET decision = %s, decided_by = %s, decision_reason = %s"
                         + ("" if fetched else ", fetch_state = CASE WHEN fetch_state = 'failed' AND %s = 'queued' "
                                               "THEN fetch_state ELSE %s END") + " WHERE id = %s",
              (("crawl" if decision.verdict == "crawl" else "skip"), decision.by, (decision.reason or "")[:500] or None,
               *(() if fetched else (state, state)), row_id))

    def _child_kind(self, from_kind, url):
        if from_kind == "json" and not is_pdf_link(url) and self.prefix:
            parts = urlsplit(url)
            if (parts.hostname or "") == (urlsplit(self.start_url).hostname or "") and parts.path.startswith(self.prefix):
                return "json"
        return "page"

    def _children(self, from_id, depth, links, context, from_kind):
        unsure, first_text = [], {}
        for url, text in links:
            first_text.setdefault(_seen_key(url), (url, text))
        for url, text in first_text.values():
            kind = self._child_kind(from_kind, url)
            if from_kind == "json" and kind == "page" and not is_pdf_link(url):
                continue  # a feed is walked for its own API and its documents, nothing else
            if not in_scope(url, self.approved):
                if is_pdf_link(url):
                    try:
                        row = self._row(url, depth, from_id, state="out_of_scope")
                    except ValueError:
                        continue  # an address with no usable host name: nothing to record
                    self._link(from_id, row[0], text)
                continue
            row = self._row(url, depth, from_id)
            row_id, _, stored_decision, decided_by, fetched, url = row
            self._link(from_id, row_id, text)
            if _seen_key(url) in self.seen:
                continue
            if depth > self.max_depth:
                if not fetched:
                    _rows(self.conn, "UPDATE pages SET fetch_state = 'beyond_depth' WHERE id = %s "
                                     "AND fetch_state IN ('queued', 'over_limit', 'ask_ai')", (row_id,))
                continue
            if decided_by == "user":
                decision = Decision(stored_decision, "your verdict", "user", "skipped_user")
            elif from_kind == "json":
                decision = Decision("crawl", "listed in a document feed", "rule")
            elif decided_by == "ai" and stored_decision:
                decision = Decision(stored_decision, None, "ai", "skipped_ai")
            else:
                decision = decide(url, text, keywords=self.keywords, page_is_article=context.get("article", False))
            if decision.verdict == "unsure":
                unsure.append((url, text, row_id, fetched))
                continue
            if decision.reason is not None or decision.by != "ai":
                self._set(row_id, fetched, decision, "queued" if decision.verdict == "crawl" else decision.state)
            if decision.verdict == "crawl":
                self._enqueue(url, depth, from_id, kind, decision)
        if unsure:
            self._ask(from_id, depth, unsure, context)

    def _enqueue(self, url, depth, from_id, kind, decision):
        if is_pdf_link(url):
            priority = PDF_FIRST
        elif kind == "json" or decision.preferred:
            priority = PREFERRED
        else:
            priority = OTHER
        self.push(url, depth, from_id, kind, priority)

    def _ask(self, from_id, depth, unsure, context):
        asked = [(url, text) for url, text, _, _ in unsure]
        answers = {}
        if self.link_checker is not None:
            answers = self.link_checker.check(self.conn, self.brand_id, from_id,
                                              {k: context.get(k, "") for k in ("url", "title", "heading")}, asked)
        touched = set()
        for url, text, row_id, fetched in unsure:
            answer = answers.get(url)
            if answer is None:
                if not fetched:
                    _rows(self.conn, "UPDATE pages SET fetch_state = 'ask_ai', decision = NULL, decided_by = NULL "
                                     "WHERE id = %s", (row_id,))
                continue
            crawl, reason = answer
            decision = Decision("crawl" if crawl else "skip", reason, "ai", None if crawl else "skipped_ai")
            self._set(row_id, fetched, decision, "queued" if crawl else "skipped_ai")
            touched.add(((urlsplit(url).hostname or "").lower(), parent_folder(url)))
            if crawl:
                self._enqueue(url, depth, from_id, "page", decision)
        for host, folder in touched:
            self._learn(host, folder)

    def _learn(self, host, folder):
        """After LEARN_AFTER matching AI answers for one host's folder, and none the other way, a rule decides for free."""
        if not folder or not all(is_valid_keyword(part) and not part.endswith("*") for part in folder.split("/")):
            return
        if any(k.host == host and k.keyword == folder for k in self.keywords):
            return
        votes = {"crawl": 0, "skip": 0}
        for url, decision in _rows(
                self.conn, "SELECT p.url, p.decision FROM pages p JOIN hosts h ON h.id = p.host_id "
                           "WHERE p.brand_id = %s AND h.host = %s AND p.decided_by = 'ai' AND p.decision IS NOT NULL",
                (self.brand_id, host)):
            if parent_folder(url) == folder:
                votes[decision] += 1
        for verdict, other in (("crawl", "skip"), ("skip", "crawl")):
            if votes[verdict] >= LEARN_AFTER and votes[other] == 0:
                effect = "prefer" if verdict == "crawl" else "skip"
                made = _rows(self.conn, "INSERT INTO crawl_keywords (keyword, effect, brand_id, host, source, note) "
                                        "VALUES (%s, %s, %s, %s, 'learned', %s) ON CONFLICT ON CONSTRAINT "
                                        "crawl_keywords_one_rule DO NOTHING RETURNING id",
                             (folder, effect, self.brand_id, host, f"learned from {votes[verdict]} AI answers"))
                if made:  # a rule the user switched off stays off
                    self.keywords.append(Keyword(folder, effect, host, "learned"))
