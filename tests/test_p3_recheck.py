"""P3: the 30-day re-check reaches the children of a page that did not change.

The mechanism is the builder's choice (links read back from stored text, a parent column on pages,
or something else). These tests only look at what the server sees. Assumed behaviour: a run walks
the whole tree below each start URL. A page that is due is fetched again; a page that is not due,
or came back unchanged, is not fetched but its children are still looked at from what was stored.
"""
import hashlib

import pytest

from p3support import q
from test_p2_fetch import World

HTML = {"Content-Type": "text/html"}
DAY = 24 * 3600


def page(*links, note=""):
    return (f"<html><body>{note}" + "".join(f'<a href="{h}">x</a>' for h in links) + "</body></html>").encode()


@pytest.fixture
def tree(runtime_conn):
    from crawler.brands import add_start_url
    w = World(runtime_conn)
    w.approve("ruud.test")
    add_start_url(runtime_conn, w.brand, w.url("ruud.test", "/start"))
    state = {"b_calls": 0, "c": page(note="first")}

    def b(rec):
        state["b_calls"] += 1
        return (500, {}, b"down") if state["b_calls"] == 1 else (200, HTML, page(note="b is back"))

    w.site.route("/start", page("/a", "/b"), headers=HTML)
    w.site.route("/a", page("/c"), headers=HTML)
    w.site.route("/b", b)
    w.site.route("/c", lambda rec: (200, HTML, state["c"]))
    w.state = state
    try:
        yield w
    finally:
        w.site.stop()


def crawl(w):
    from crawler.crawl import run
    from p11support import Checker
    run(w.conn, w.fetcher(), brand=w.brand, link_checker=Checker())


def counts(w):
    return {p: len(w.site.hits(p)) for p in ("/start", "/a", "/b", "/c")}


def test_the_first_run_reaches_every_page(tree):
    crawl(tree)
    assert counts(tree) == {"/start": 1, "/a": 1, "/b": 1, "/c": 1}


def test_after_30_days_unchanged_parents_still_lead_to_their_children(tree):
    crawl(tree)
    tree.clock.advance(31 * DAY)
    tree.state["c"] = page(note="second")  # only the grandchild changed
    crawl(tree)
    assert counts(tree) == {"/start": 2, "/a": 2, "/b": 2, "/c": 2}
    sha = q(tree.conn, "SELECT content_sha FROM pages WHERE brand_id = %s AND url LIKE '%%/c'", (tree.brand_id,))[0][0]
    assert sha == hashlib.sha256(page(note="second")).hexdigest()


def test_after_two_days_only_the_failed_page_is_tried_again(tree):
    crawl(tree)
    tree.clock.advance(2 * DAY)
    crawl(tree)
    assert counts(tree) == {"/start": 1, "/a": 1, "/b": 2, "/c": 1}
    assert q(tree.conn, "SELECT http_status FROM pages WHERE brand_id = %s AND url LIKE '%%/b'", (tree.brand_id,)) == ((200,),)


def test_an_hour_later_nothing_is_fetched_again(tree):
    crawl(tree)
    tree.clock.advance(3600)
    crawl(tree)
    assert counts(tree) == {"/start": 1, "/a": 1, "/b": 1, "/c": 1}
    assert tree.site.hits("/robots.txt")  # the first run did reach the server, so "no new hits" means something
