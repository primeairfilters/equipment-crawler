"""P4: free discovery from `Sitemap:` lines in an approved host's cached robots.txt.

read_sitemaps(conn, fetcher, brand_name, *, max_urls=5000) -> int (start URLs added). It never commits.
It reads only robots.txt text that is already cached in the hosts table (a normal fetch caches it),
fetches sitemaps through the same polite, safe Fetcher, and adds start_urls with found_by 'sitemap'.
"""
import pytest

from p3support import q
from test_p2_fetch import World

XML = {"Content-Type": "application/xml"}
SECTION = "/products/hvac/air_conditioners/"


def urlset(*locs):
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + body + "</urlset>").encode()


def index(*locs):
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return ('<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + body + "</sitemapindex>").encode()


@pytest.fixture
def w(runtime_conn):
    from crawler.brands import add_start_url
    world = World(runtime_conn)
    world.approve("ruud.test")
    add_start_url(runtime_conn, world.brand, world.url("ruud.test", SECTION))
    world.site.route("/p", b"<html><body>hello</body></html>", headers={"Content-Type": "text/html"})
    assert world.fetcher().fetch(world.url("ruud.test", "/p"), world.brand_id).outcome == "fetched"  # caches robots.txt
    world.site.requests.clear()
    try:
        yield world
    finally:
        world.site.stop()


def set_robots(world, *lines, host="ruud.test"):
    body = "User-agent: *\nDisallow:\n" + "".join(line + "\n" for line in lines)
    with world.conn.cursor() as cur:
        cur.execute("UPDATE hosts SET robots_body = %s, robots_status = 200, robots_fetched_at = %s WHERE host = %s",
                    (body, world.clock.now(), host))


def read(world, **kw):
    from crawler.sitemaps import read_sitemaps
    return read_sitemaps(world.conn, world.fetcher(), world.brand, **kw)


def sitemap_urls(world):
    return [r[0] for r in q(world.conn, "SELECT url FROM start_urls WHERE brand_id = %s AND found_by = 'sitemap' ORDER BY id",
                            (world.brand_id,))]


def under(world, name):
    return world.url("ruud.test", SECTION + name)


def test_the_default_cap_is_5000_urls_per_sitemap():
    from crawler.sitemaps import MAX_SITEMAP_URLS
    assert MAX_SITEMAP_URLS == 5000


def test_urls_under_a_start_urls_section_are_added_and_the_rest_are_not(w):
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(
        under(w, "ra15az"), under(w, "ra16bx"),
        w.url("ruud.test", "/products/hvac/furnaces/g1"), w.url("ruud.test", "/about"),
        w.url("ruud.test", "/products/hvac/air_conditioners")),  # the section itself without the slash is not under it
        headers=XML)
    added = read(w)
    assert added == 2
    assert sitemap_urls(w) == [under(w, "ra15az"), under(w, "ra16bx")]
    assert w.site.hits("/sitemap.xml")  # it was fetched, so the filtering above is what kept the others out


def test_running_it_again_adds_nothing_new(w):
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(under(w, "a"), under(w, "b")), headers=XML)
    assert read(w) == 2
    assert read(w) == 0
    assert len(sitemap_urls(w)) == 2


def test_a_start_url_the_user_added_stays_the_users(w):
    from crawler.brands import add_start_url
    add_start_url(w.conn, w.brand, under(w, "a"))
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(under(w, "a"), under(w, "b")), headers=XML)
    assert read(w) == 1
    assert q(w.conn, "SELECT found_by FROM start_urls WHERE brand_id = %s AND url = %s", (w.brand_id, under(w, "a"))) == (("user",),)


def test_sitemap_lines_are_found_whatever_their_case_and_every_one_is_read(w):
    set_robots(w, f"SITEMAP: {w.url('ruud.test', '/one.xml')}", f"sitemap:{w.url('ruud.test', '/two.xml')}")
    w.site.route("/one.xml", urlset(under(w, "a")), headers=XML)
    w.site.route("/two.xml", urlset(under(w, "b")), headers=XML)
    assert read(w) == 2


def test_a_sitemap_on_an_unapproved_host_is_not_fetched(w):
    set_robots(w, f"Sitemap: {w.url('other.test', '/sitemap.xml')}", f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(under(w, "a")), headers=XML)
    assert read(w) == 1                                  # the approved one was read, so the code ran
    assert w.site.hits(host="other.test") == []


def test_only_approved_hosts_robots_are_read(w):
    with w.conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, 'waiting.test', 'waiting', 'ai')",
                    (w.brand_id,))
        cur.execute("INSERT INTO hosts (host, robots_status, robots_body, robots_fetched_at) VALUES ('waiting.test', 200, %s, %s)",
                    (f"User-agent: *\nDisallow:\nSitemap: {w.url('waiting.test', '/sitemap.xml')}\n", w.clock.now()))
    w.site.route("/sitemap.xml", urlset(under(w, "a")), headers=XML)
    assert read(w) == 0
    assert w.site.requests == []


def test_a_sitemap_index_is_followed_one_level_only(w):
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/index.xml')}")
    w.site.route("/index.xml", index(w.url("ruud.test", "/s1.xml"), w.url("ruud.test", "/s2.xml")), headers=XML)
    w.site.route("/s1.xml", urlset(under(w, "a")), headers=XML)
    w.site.route("/s2.xml", index(w.url("ruud.test", "/s3.xml")), headers=XML)  # an index inside an index
    w.site.route("/s3.xml", urlset(under(w, "deep")), headers=XML)
    assert read(w) == 1
    assert sitemap_urls(w) == [under(w, "a")]
    assert w.site.hits("/s2.xml")        # the second level was looked at
    assert w.site.hits("/s3.xml") == []  # the third was not


def test_at_most_max_urls_are_taken_from_one_sitemap(w):
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(*[under(w, f"m{n:02d}") for n in range(12)]), headers=XML)
    assert read(w, max_urls=5) == 5
    assert sitemap_urls(w) == [under(w, f"m{n:02d}") for n in range(5)]


def test_other_schemes_and_unapproved_hosts_inside_a_sitemap_are_dropped(w):
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(
        under(w, "good"), "ftp://ruud.test" + SECTION + "x", "javascript:alert(1)",
        w.url("evilruud.test", SECTION + "x"), w.url("ruud.test.evil.test", SECTION + "x")), headers=XML)
    assert read(w) == 1
    assert sitemap_urls(w) == [under(w, "good")]


def test_a_brand_with_no_start_urls_gets_nothing_from_a_sitemap(w):
    with w.conn.cursor() as cur:
        cur.execute("UPDATE start_urls SET status = 'stopped' WHERE brand_id = %s", (w.brand_id,))
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(under(w, "a")), headers=XML)
    assert read(w) == 0
    assert sitemap_urls(w) == []


def test_a_missing_sitemap_is_not_an_error(w):
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/gone.xml')}", f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(under(w, "a")), headers=XML)
    assert read(w) == 1
    assert w.site.hits("/gone.xml")


# ---------------------------------------------------------------- hostile XML

def test_an_external_entity_is_refused_and_never_fetched_and_other_sitemaps_still_work(w):
    secret = w.url("ruud.test", "/secret")
    evil = (f'<?xml version="1.0"?><!DOCTYPE urlset [<!ENTITY xxe SYSTEM "{secret}">]>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&xxe;</loc></url></urlset>').encode()
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/evil.xml')}", f"Sitemap: {w.url('ruud.test', '/good.xml')}")
    w.site.route("/evil.xml", evil, headers=XML)
    w.site.route("/secret", under(w, "from-the-entity").encode(), headers={"Content-Type": "text/plain"})
    w.site.route("/good.xml", urlset(under(w, "good")), headers=XML)
    assert read(w) == 1
    assert sitemap_urls(w) == [under(w, "good")]
    assert w.site.hits("/evil.xml")                 # the bad one really was fetched and parsed
    assert w.site.hits("/secret") == []


def test_an_entity_bomb_is_refused_and_other_sitemaps_still_work(w):
    lol = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
           '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
           '<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">]>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&lol4;</loc></url></urlset>').encode()
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/bomb.xml')}", f"Sitemap: {w.url('ruud.test', '/good.xml')}")
    w.site.route("/bomb.xml", lol, headers=XML)
    w.site.route("/good.xml", urlset(under(w, "good")), headers=XML)
    assert read(w) == 1
    assert sitemap_urls(w) == [under(w, "good")]
    assert w.site.hits("/bomb.xml")


def test_something_that_is_not_xml_is_skipped(w):
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/notxml.xml')}", f"Sitemap: {w.url('ruud.test', '/good.xml')}")
    w.site.route("/notxml.xml", b"<html>this is not a sitemap", headers=XML)
    w.site.route("/good.xml", urlset(under(w, "good")), headers=XML)
    assert read(w) == 1
    assert w.site.hits("/notxml.xml")


def test_a_start_address_from_a_sitemap_follows_one_link_deep(w):
    """P11 (D60): every listed page is its own start address, so it only needs its own links (its PDFs), not depth 3."""
    set_robots(w, f"Sitemap: {w.url('ruud.test', '/sitemap.xml')}")
    w.site.route("/sitemap.xml", urlset(under(w, "a")), headers=XML)
    assert read(w) == 1
    assert q(w.conn, "SELECT depth FROM start_urls WHERE brand_id = %s AND url = %s", (w.brand_id, under(w, "a"))) == ((1,),)
