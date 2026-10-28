"""Helpers shared by the P11 gathering tests: a crawl world, link checkers that stand in for the AI, row readers."""
import pytest

from test_p2_fetch import World

HTML = {"Content-Type": "text/html"}
PDF = {"Content-Type": "application/pdf"}


class Checker:
    """Stands in for the AI link check. answer(url, text) -> True (crawl), False (skip) or None (no answer)."""

    def __init__(self, answer=lambda url, text: True):
        self.answer = answer
        self.calls = []  # one entry per page: (context, [(url, text), ...])

    def check(self, conn, brand_id, page_id, context, links):
        self.calls.append((context, list(links)))
        out = {}
        for url, text in links:
            verdict = self.answer(url, text)
            if verdict is not None:
                out[url] = (verdict, "fake " + ("yes" if verdict else "no"))
        return out

    def asked(self):
        return [url for _, links in self.calls for url, _ in links]


@pytest.fixture
def world(runtime_conn):
    w = World(runtime_conn)
    w.approve("ruud.test")
    try:
        yield w
    finally:
        w.site.stop()


def page(*links, title="page", body=""):
    """links: href, or (href, text)."""
    anchors = "".join(f'<a href="{l[0]}">{l[1]}</a>' if isinstance(l, tuple) else f'<a href="{l}">link</a>' for l in links)
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}{anchors}</body></html>".encode()


def serve(world, path, *links, host="*", title=None):
    world.site.route(path, page(*links, title=title or path), headers=HTML, host=host)


def serve_pdf(world, path, body=None, host="*"):
    from p3support import make_pdf
    world.site.route(path, body or make_pdf([[f"Document {path}", "Model RA15AZ24AJ3CA"]]), headers=PDF, host=host)


def start(world, path="/start", depth=None, host="ruud.test", kind="page"):
    from crawler.brands import add_start_url
    return add_start_url(world.conn, world.brand, world.url(host, path), depth=depth, kind=kind)


def crawl(world, checker="yes", **kwargs):
    """checker: 'yes' (a fake AI that says crawl to everything), None (no AI at all), or a Checker."""
    from crawler.crawl import run
    if checker == "yes":
        checker = Checker()
    return run(world.conn, world.fetcher(), brand=kwargs.pop("brand", world.brand), link_checker=checker, **kwargs)


def paths(world):
    return [r["path"] for r in world.site.page_hits()]


def state(world, path, host="ruud.test"):
    rows = world.one("SELECT fetch_state, decided_by, decision_reason FROM pages WHERE brand_id = %s AND url = %s",
                     (world.brand_id, world.url(host, path)))
    return rows[0] if rows else None


def links_from(world, path):
    return set(world.one(
        "SELECT t.url, l.link_text FROM page_links l JOIN pages f ON f.id = l.from_page_id JOIN pages t ON t.id = l.to_page_id "
        "WHERE f.brand_id = %s AND f.url = %s", (world.brand_id, world.url("ruud.test", path))))


def skip_keyword(world, keyword, effect="skip", brand=None, host=None, source="user"):
    with world.conn.cursor() as cur:
        cur.execute("INSERT INTO crawl_keywords (keyword, effect, brand_id, host, source) VALUES (%s, %s, %s, %s, %s)",
                    (keyword, effect, brand, host, source))
