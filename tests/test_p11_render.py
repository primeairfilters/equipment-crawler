"""P11 (D68a): pages on a host switched to the browser are rendered in headless Chromium, honestly and safely.

The browser runs in a child process with no secrets and reaches the network only through a proxy in our process,
which resolves each host once, refuses private addresses, and pins the connection to the checked address.
Navigation is limited to approved hosts. Everything else on the page still loads, minus images, media and fonts.
"""
import pytest

from p11support import HTML, crawl, paths, start, world  # noqa: F401

JS_PAGE = b"""<html><head><title>Product</title></head><body><div id="app">Loading...</div>
<script src="/app.js"></script></body></html>"""
APP_JS = b"""document.getElementById('app').innerHTML =
  '<h1>RA15AZ rendered by script</h1><a href="/doc.pdf">Installation manual</a>';"""


def browser_host(world, host="ruud.test"):
    from crawler.brands import approve_host
    approve_host(world.conn, host, use_browser=True)


def page_text(world, path):
    rows = world.one("SELECT body FROM page_text pt JOIN pages p ON p.id = pt.page_id WHERE p.url = %s",
                     (world.url("ruud.test", path),))
    return rows[0][0] if rows else ""


@pytest.fixture
def js_site(world):
    world.site.route("/start", JS_PAGE, headers=HTML)
    world.site.route("/app.js", APP_JS, headers={"Content-Type": "application/javascript"})
    world.site.route("/doc.pdf", b"%PDF-1.4\n%%EOF\n", headers={"Content-Type": "application/pdf"})
    start(world)
    return world


def test_a_browser_host_is_rendered_and_links_made_by_script_are_followed(js_site):
    from crawler.fetch import USER_AGENT
    browser_host(js_site)
    crawl(js_site)
    assert "RA15AZ rendered by script" in page_text(js_site, "/start")
    assert "/doc.pdf" in paths(js_site)
    browser_hits = [r for r in js_site.site.requests if r["path"] in ("/start", "/app.js")]
    assert browser_hits and all(r["headers"].get("user-agent") == USER_AGENT for r in browser_hits)


def test_without_the_switch_the_same_page_is_plain_http_and_no_browser_starts(js_site):
    calls = []

    class NoBrowser:
        def render(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("the browser must not start for a plain host")

    from crawler.crawl import run
    from p11support import Checker
    fetcher = js_site.fetcher()
    fetcher.renderer = NoBrowser()
    run(js_site.conn, fetcher, brand=js_site.brand, link_checker=Checker())
    assert calls == []
    assert "Loading..." in page_text(js_site, "/start") and "rendered by script" not in page_text(js_site, "/start")
    assert js_site.site.hits("/app.js") == []  # plain HTTP never runs scripts


def test_the_browser_cannot_reach_a_private_address(world):
    world.special_ips["metadata.test"] = "169.254.169.254"
    world.site.route("/start", b"""<html><body><p>shown</p>
        <script>fetch('http://metadata.test/latest/meta-data/').then(r => r.text())
          .then(t => document.body.insertAdjacentHTML('beforeend', '<p>LEAKED ' + t + '</p>'))
          .catch(() => document.body.insertAdjacentHTML('beforeend', '<p>blocked</p>'));</script></body></html>""",
                     headers=HTML)
    start(world)
    browser_host(world)
    crawl(world)
    text = page_text(world, "/start")
    assert "shown" in text and "LEAKED" not in text
    assert world.site.hits(host="metadata.test") == []


def test_the_browser_never_navigates_to_a_host_that_is_not_approved(world):
    world.site.route("/start", b"""<html><body><p>first</p>
        <script>window.location = 'http://elsewhere.test/landing';</script></body></html>""", headers=HTML)
    world.site.route("/landing", b"<html><body>elsewhere</body></html>", headers=HTML)
    start(world)
    browser_host(world)
    crawl(world)
    assert world.site.hits(host="elsewhere.test") == []


def test_two_browser_pages_on_one_host_keep_the_ten_second_gap(world):
    world.site.route("/start", b'<html><body><a href="/b">Specifications</a></body></html>', headers=HTML)
    world.site.route("/b", b"<html><body>b</body></html>", headers=HTML)
    start(world)
    browser_host(world)
    crawl(world)
    pages = [r for r in world.site.requests if r["path"] in ("/start", "/b")]
    assert [r["path"] for r in pages] == ["/start", "/b"]
    assert (pages[1]["t"] - pages[0]["t"]).total_seconds() >= 10


def test_the_proxy_refuses_a_private_address_and_connects_to_the_checked_one(world):
    """The proxy itself, without a browser: the same request is refused or served depending only on the address check."""
    import httpx
    from crawler.render import SafeProxy
    world.site.route("/inside", b"secret", headers=HTML)
    resolver = lambda host: ["127.0.0.1"]  # noqa: E731  (inside.test points at a loopback service)
    with SafeProxy(resolver, ip_allowed=lambda ip: False) as proxy:
        with httpx.Client(proxy=f"http://127.0.0.1:{proxy.port}", trust_env=False) as client:
            refused = client.get(world.url("inside.test", "/inside"))
    assert refused.status_code == 403 and proxy.refused == ["inside.test"]
    assert world.site.hits("/inside") == []
    with SafeProxy(resolver, ip_allowed=lambda ip: True) as proxy:  # control: allowed, it reaches the checked address
        with httpx.Client(proxy=f"http://127.0.0.1:{proxy.port}", trust_env=False) as client:
            served = client.get(world.url("inside.test", "/inside"))
    assert served.status_code == 200 and served.content == b"secret"
    assert world.site.hits("/inside")[0]["host"] == "inside.test"


def test_the_proxy_refuses_methods_other_than_get_and_head(world):
    import httpx
    from crawler.render import SafeProxy
    with SafeProxy(lambda host: ["127.0.0.1"], ip_allowed=lambda ip: True) as proxy:
        with httpx.Client(proxy=f"http://127.0.0.1:{proxy.port}", trust_env=False) as client:
            assert client.post(world.url("ruud.test", "/x"), content=b"data").status_code == 405
    assert world.site.requests == []
