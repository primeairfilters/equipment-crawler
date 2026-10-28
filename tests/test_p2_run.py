"""P2: `run` follows links from a brand's start URLs, politely and inside its limits.

Same fake world as test_p2_fetch.py: an in-process server, a fake clock, rolled-back rows.
Every test restricts run() to its own brand, so rows already committed in the testing
database cannot add requests to the count.
"""
import pytest

from fakesite import FakeSite  # noqa: F401  (documented dependency)
from test_p2_fetch import World

HTML_HEADERS = {"Content-Type": "text/html"}


def page(*links, title="page"):
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    return f"<html><head><title>{title}</title></head><body>{anchors}</body></html>".encode()


@pytest.fixture
def world(runtime_conn):
    w = World(runtime_conn)
    w.approve("ruud.test")
    try:
        yield w
    finally:
        w.site.stop()


def serve(world, path, *links, host="*"):
    world.site.route(path, page(*links, title=path), headers=HTML_HEADERS, host=host)


def start(world, path="/start", brand=None):
    from crawler.brands import add_start_url
    add_start_url(world.conn, brand or world.brand, world.url("ruud.test", path))


def crawl(world, **kwargs):
    """A crawl with a stand-in AI link check that says yes to every link the rules are unsure about (P11)."""
    from crawler.crawl import run
    from p11support import Checker
    kwargs.setdefault("link_checker", Checker())
    return run(world.conn, world.fetcher(), brand=kwargs.pop("brand", world.brand), **kwargs)


def paths(world):
    return [r["path"] for r in world.site.page_hits()]


def one(world, sql, args=()):
    return world.one(sql, args)


def test_run_fetches_the_start_url_and_follows_relative_and_absolute_links(world):
    start(world)
    serve(world, "/start", "/a", "b", world.url("ruud.test", "/c"))
    for p in ("/a", "/b", "/c"):
        serve(world, p)
    crawl(world)
    assert sorted(paths(world)) == ["/a", "/b", "/c", "/start"]
    assert one(world, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND http_status = 200",
               (world.brand_id,))[0][0] == 4


def test_links_are_followed_at_most_three_deep_by_default(world):
    start(world)
    serve(world, "/start", "/a")
    serve(world, "/a", "/b")
    serve(world, "/b", "/c")
    serve(world, "/c", "/d")
    serve(world, "/d", "/e")
    crawl(world)
    assert sorted(paths(world)) == ["/a", "/b", "/c", "/start"]  # /d is depth 4 (D60: the default is 3)
    depths = dict(one(world, "SELECT split_part(url, '/', -1), depth FROM pages WHERE brand_id = %s",
                      (world.brand_id,)))
    assert depths == {"start": 0, "a": 1, "b": 2, "c": 3, "d": 4}  # /d is recorded beyond depth, never fetched


def test_max_depth_is_a_setting_of_the_start_address(world):
    from crawler.brands import add_start_url
    add_start_url(world.conn, world.brand, world.url("ruud.test", "/start"), depth=1)
    serve(world, "/start", "/a")
    serve(world, "/a", "/b")
    serve(world, "/b")
    crawl(world)
    assert sorted(paths(world)) == ["/a", "/start"]


def test_pages_remember_which_start_url_they_came_from(world):
    start(world)
    serve(world, "/start", "/a")
    serve(world, "/a")
    crawl(world)
    rows = one(world, "SELECT p.url, s.url FROM pages p JOIN start_urls s ON s.id = p.start_url_id "
                      "WHERE p.brand_id = %s", (world.brand_id,))
    assert len(rows) == 2
    assert {r[1] for r in rows} == {world.url("ruud.test", "/start")}


def test_only_approved_hosts_are_followed_and_others_are_never_requested(world):
    start(world)
    serve(world, "/start",
          world.url("shop.ruud.test", "/sub"),        # true subdomain: followed
          world.url("evilruud.test", "/x"),           # look-alike
          world.url("ruud.test.evil.test", "/y"),     # look-alike
          world.url("elsewhere.test", "/z"),          # never added
          "mailto:sales@ruud.test",
          "javascript:void(0)",
          "ftp://ruud.test/file")
    serve(world, "/sub")
    crawl(world)
    assert sorted(paths(world)) == ["/start", "/sub"]
    asked_for = {r["host"] for r in world.site.requests}
    assert asked_for == {"ruud.test", "shop.ruud.test"}
    assert world.site.hits("/sub", host="shop.ruud.test")  # control: the in-scope link did go through


def test_a_link_to_a_host_still_waiting_for_ok_is_not_requested(world):
    with world.conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) "
                    "VALUES (%s, 'files.ruud.test.waiting.test', 'waiting', 'ai')", (world.brand_id,))
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) "
                    "VALUES (%s, 'docs.test', 'waiting', 'ai')", (world.brand_id,))
    start(world)
    serve(world, "/start", world.url("docs.test", "/manual"), "/a")
    serve(world, "/a")
    crawl(world)
    assert sorted(paths(world)) == ["/a", "/start"]
    assert world.site.hits(host="docs.test") == []


def test_ahri_links_are_never_requested(world):
    start(world)
    serve(world, "/start", world.url("www.ahridirectory.org", "/search"), "/a")
    serve(world, "/a")
    crawl(world)
    assert sorted(paths(world)) == ["/a", "/start"]
    assert world.site.hits(host="www.ahridirectory.org") == []


def test_a_page_is_fetched_once_per_run_even_if_linked_many_times(world):
    start(world)
    serve(world, "/start", "/a", "/a", "/a#top", "/b", "/start", "/start#again")
    serve(world, "/a", "/b", "/start", "/a")
    serve(world, "/b", "/a", "/start")
    crawl(world)
    seen = paths(world)
    assert sorted(seen) == ["/a", "/b", "/start"]  # each exactly once, no repeats in the log
    assert one(world, "SELECT COUNT(*) FROM pages WHERE brand_id = %s", (world.brand_id,))[0][0] == 3


def test_the_page_cap_per_start_url_is_exact(world):
    start(world)
    links = [f"/p{n}" for n in range(12)]
    serve(world, "/start", *links)
    for p in links:
        serve(world, p)
    crawl(world, max_pages=5)
    assert len(paths(world)) == 5            # the start URL and four links, no more
    assert paths(world)[0] == "/start"
    assert one(world, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND fetched_at IS NOT NULL",
               (world.brand_id,))[0][0] == 5
    assert one(world, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND fetch_state = 'over_limit'",
               (world.brand_id,))[0][0] == 8  # the rest are recorded, not fetched (P11)


def test_the_cap_is_counted_per_start_url_not_per_brand(world):
    start(world, "/s1")
    start(world, "/s2")
    for s, tag in (("/s1", "a"), ("/s2", "b")):
        links = [f"/{tag}{n}" for n in range(8)]
        serve(world, s, *links)
        for p in links:
            serve(world, p)
    crawl(world, max_pages=3)
    seen = paths(world)
    assert len(seen) == 6
    assert len([p for p in seen if p.startswith(("/s1", "/a"))]) == 3
    assert len([p for p in seen if p.startswith(("/s2", "/b"))]) == 3


def test_a_pdf_is_fetched_but_not_searched_for_links(world):
    start(world)
    serve(world, "/start", "/doc.pdf", "/a")
    serve(world, "/a")
    serve(world, "/trap")
    pdf = b"%PDF-1.4\n<a href='/trap'>trap</a> /trap http://ruud.test/trap\n%%EOF\n"
    world.site.route("/doc.pdf", pdf, headers={"Content-Type": "application/pdf"})
    crawl(world)
    assert sorted(paths(world)) == ["/a", "/doc.pdf", "/start"]
    assert "/trap" not in paths(world)
    assert one(world, "SELECT is_pdf FROM pages WHERE brand_id = %s AND url LIKE '%%doc.pdf'",
               (world.brand_id,)) == ((True,),)


def test_a_failing_link_does_not_stop_the_rest_of_the_crawl(world):
    start(world)
    serve(world, "/start", "/gone", "/boom", "/ok")
    world.site.route("/boom", b"x", status=500)
    serve(world, "/ok")
    crawl(world)
    assert "/ok" in paths(world)
    assert "/gone" in paths(world)
    assert one(world, "SELECT http_status FROM pages WHERE brand_id = %s AND url LIKE '%%/gone'",
               (world.brand_id,)) == ((404,),)


def test_a_stopped_brand_is_skipped_and_an_active_one_still_runs(world):
    from crawler.brands import add_brand, add_start_url, approve_host, stop
    other = f"{world.brand} other"
    add_brand(world.conn, other)
    approve_host(world.conn, "ruud.test", brand=other)
    start(world, "/stopped-start")
    add_start_url(world.conn, other, world.url("ruud.test", "/live-start"))
    serve(world, "/stopped-start", "/stopped-link")
    serve(world, "/stopped-link")
    serve(world, "/live-start")
    stop(world.conn, world.brand)

    crawl(world, brand=world.brand)
    assert world.site.requests == []  # the stopped brand: nothing at all, not even robots.txt

    crawl(world, brand=other)  # control: the path runs for an active brand
    assert paths(world) == ["/live-start"]


def test_a_stopped_start_url_is_skipped(world):
    start(world, "/start")
    serve(world, "/start")
    with world.conn.cursor() as cur:
        cur.execute("UPDATE start_urls SET status = 'stopped' WHERE brand_id = %s", (world.brand_id,))
    crawl(world)
    assert world.site.requests == []


def test_a_start_url_on_a_host_that_was_never_approved_is_skipped_without_error(world):
    from crawler.brands import add_start_url
    add_start_url(world.conn, world.brand, world.url("unapproved.test", "/start"))
    start(world, "/start")
    serve(world, "/start")
    crawl(world)
    assert paths(world) == ["/start"]
    assert world.site.hits(host="unapproved.test") == []
