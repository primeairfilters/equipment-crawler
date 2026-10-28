"""P2 review fixes: stacked or unknown encodings, odd IPv6 forms, robots redirects, the
Retry-After ceiling, the failure count after a rest, and the run() after_page callback.

Same fake world as test_p2_fetch.py (in-process server, fake clock, rolled-back rows).
"""
import pytest

from fakesite import gzip_bytes
from test_p2_fetch import HTML, World, get, host_times

HTML_HEADERS = {"Content-Type": "text/html"}


@pytest.fixture
def world(runtime_conn):
    w = World(runtime_conn)
    try:
        yield w
    finally:
        w.site.stop()


@pytest.fixture
def ruud(world):
    world.approve("ruud.test")
    world.site.route("/p", HTML, headers=HTML_HEADERS)
    return world


# ---------------------------------------------------------------- Content-Encoding

def test_stacked_gzip_is_refused_and_nothing_is_decoded(ruud):
    stacked = gzip_bytes(gzip_bytes(HTML))
    ruud.site.route("/gg", stacked, headers={**HTML_HEADERS, "Content-Encoding": "gzip, gzip"})
    result = get(ruud, "/gg")
    assert (result.outcome, result.reason) == ("failed", "bad_content_type")
    assert not result.body
    assert ruud.site.hits("/gg")  # the request did go out, so the refusal is the fetcher's
    assert ruud.one("SELECT content_sha FROM pages WHERE brand_id = %s AND url LIKE '%%/gg'",
                    (ruud.brand_id,)) == ((None,),)  # no body was kept or hashed


def test_an_unknown_encoding_is_refused(ruud):
    ruud.site.route("/br", b"\x0b\x02\x80abc", headers={**HTML_HEADERS, "Content-Encoding": "br"})
    result = get(ruud, "/br")
    assert (result.outcome, result.reason) == ("failed", "bad_content_type")
    assert not result.body
    assert ruud.site.hits("/br")


def test_a_single_gzip_layer_still_works(ruud):
    ruud.site.route("/g", gzip_bytes(HTML), headers={**HTML_HEADERS, "Content-Encoding": "gzip"})
    result = get(ruud, "/g")
    assert result.outcome == "fetched"
    assert result.body == HTML


@pytest.mark.parametrize("encoding,body", [
    ("br", b"User-agent: *\nDisallow: /\n"),
    ("gzip, gzip", gzip_bytes(gzip_bytes(b"User-agent: *\nDisallow: /\n"))),
])
def test_robots_with_an_encoding_we_cannot_read_means_blocked(ruud, encoding, body):
    ruud.site.route("/robots.txt", body, headers={"Content-Type": "text/plain", "Content-Encoding": encoding})
    result = get(ruud, "/p")
    assert (result.outcome, result.reason) == ("refused", "robots")
    assert ruud.site.hits("/robots.txt")  # robots.txt was requested, so the refusal came from reading it
    assert ruud.site.hits("/p") == []


# ---------------------------------------------------------------- address policy

@pytest.mark.parametrize("ip", ["64:ff9b::a9fe:a9fe", "::a9fe:a9fe", "fec0::1", "64:ff9b::7f00:1", "64:ff9b::a00:1"])
def test_is_public_ip_refuses_nat64_compatible_and_site_local_forms(ip):
    from crawler.netpolicy import is_public_ip
    assert is_public_ip(ip) is False


def test_is_public_ip_allows_nat64_of_a_public_address():
    from crawler.netpolicy import is_public_ip
    assert is_public_ip("64:ff9b::808:808") is True  # 8.8.8.8


# ---------------------------------------------------------------- robots.txt redirects

def redirect_robots(world, to_host):
    world.site.route("/robots.txt", status=302, host="ruud.test",
                     headers={"Location": world.url(to_host, "/robots.txt")})


def test_robots_redirected_to_an_approved_host_is_obeyed(ruud):
    ruud.approve("other.test")
    redirect_robots(ruud, "other.test")
    ruud.site.route("/robots.txt", b"User-agent: *\nDisallow: /\n", host="other.test",
                    headers={"Content-Type": "text/plain"})
    result = get(ruud, "/p")
    assert (result.outcome, result.reason) == ("refused", "robots")
    assert ruud.site.hits("/robots.txt", host="ruud.test")
    assert ruud.site.hits("/robots.txt", host="other.test")  # the redirect was followed
    assert ruud.site.hits("/p") == []


def test_robots_redirected_to_an_approved_host_that_allows_lets_the_page_through(ruud):
    ruud.approve("other.test")
    redirect_robots(ruud, "other.test")
    ruud.site.route("/robots.txt", b"User-agent: *\nDisallow:\n", host="other.test",
                    headers={"Content-Type": "text/plain"})
    assert get(ruud, "/p").outcome == "fetched"
    assert ruud.site.hits("/robots.txt", host="other.test")


def test_robots_redirected_to_an_unapproved_host_means_blocked(ruud):
    redirect_robots(ruud, "evilruud.test")
    ruud.site.route("/robots.txt", b"User-agent: *\nDisallow:\n", host="evilruud.test",
                    headers={"Content-Type": "text/plain"})
    result = get(ruud, "/p")
    assert (result.outcome, result.reason) == ("refused", "robots")
    assert ruud.site.hits("/robots.txt", host="ruud.test")  # the first hop ran
    assert ruud.site.hits(host="evilruud.test") == []
    assert ruud.site.hits("/p") == []


# ---------------------------------------------------------------- Retry-After ceiling

def test_retry_after_is_capped_at_one_hour(ruud):
    ruud.site.route("/busy", b"slow down", status=429, headers={"Retry-After": "86400"})
    assert get(ruud, "/busy").reason == "http_429"
    get(ruud, "/p")
    waited = (ruud.site.hits("/p")[0]["t"] - ruud.site.hits("/busy")[0]["t"]).total_seconds()
    assert 3600 <= waited <= 3601


# ---------------------------------------------------------------- failure count after a rest

def test_after_a_rest_expires_one_failure_does_not_start_another_rest(ruud):
    ruud.site.route("/bad", b"boom", status=500)
    for _ in range(5):
        assert get(ruud, "/bad").outcome == "failed"
    before = ruud.one("SELECT failure_count, rest_until FROM hosts WHERE host = 'ruud.test'")[0]
    assert before[0] == 5 and before[1] is not None  # the rest really started

    ruud.clock.advance(25 * 3600)
    assert get(ruud, "/bad").outcome == "failed"  # a single new failure
    after = ruud.one("SELECT failure_count, rest_until FROM hosts WHERE host = 'ruud.test'")[0]
    assert after == (1, None)
    assert get(ruud, "/p").outcome == "fetched"  # not resting


# ---------------------------------------------------------------- run(after_page=...)

def test_run_calls_after_page_once_per_page(ruud):
    from crawler.brands import add_start_url
    from crawler.crawl import run
    from p11support import Checker
    add_start_url(ruud.conn, ruud.brand, ruud.url("ruud.test", "/start"))
    ruud.site.route("/start", b'<a href="/a">a</a><a href="/b">b</a>', headers=HTML_HEADERS)
    ruud.site.route("/a", HTML, headers=HTML_HEADERS)
    ruud.site.route("/b", HTML, headers=HTML_HEADERS)
    calls = []
    run(ruud.conn, ruud.fetcher(), brand=ruud.brand, link_checker=Checker(), after_page=lambda: calls.append(1))
    pages = [r["path"] for r in ruud.site.page_hits()]
    assert sorted(pages) == ["/a", "/b", "/start"]  # the crawl ran
    assert len(calls) == 3


def test_run_without_a_callback_still_works(ruud):
    from crawler.brands import add_start_url
    from crawler.crawl import run
    from p11support import Checker
    add_start_url(ruud.conn, ruud.brand, ruud.url("ruud.test", "/p"))
    run(ruud.conn, ruud.fetcher(), brand=ruud.brand, link_checker=Checker())
    assert [r["path"] for r in ruud.site.page_hits()] == ["/p"]
