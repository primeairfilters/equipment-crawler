"""P4 details the builder reported: discover's return values, hosts implied by start URLs,
the sitemap folder rule, and the 5x price above 100k prompt tokens. Written after the code."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fakeai import FakeDiscoverAI
from p3support import new_brand, q
from test_p4_discover import GOOD, T0, domains, found, run_discover
from test_p4_sitemaps import XML, urlset
from test_p2_fetch import World

HAIKU = "claude-haiku-5-5"


def test_discover_says_why_it_did_nothing(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(GOOD)
    assert run_discover(runtime_conn, fake, name, cap_usd=Decimal("0.05")) == "budget"
    first = run_discover(runtime_conn, fake, name, cap_usd=Decimal("25"))
    assert isinstance(first, dict)  # the answer that was stored
    assert run_discover(runtime_conn, fake, name, now=T0 + timedelta(days=10)) == "recent"


def test_money_set_aside_for_extraction_batches_in_flight_counts_against_the_discovery_cap(runtime_conn):
    from p3support import make_doc
    name, brand_id = new_brand(runtime_conn)
    other_name, other_id = new_brand(runtime_conn)
    page_id = make_doc(runtime_conn, other_id, ["Some document text"])
    with runtime_conn.cursor() as cur:
        cur.execute("UPDATE pages SET ai_batch_id = 'batch_in_flight', ai_reserved_usd = %s WHERE id = %s", ("24.99", page_id))
    fake = FakeDiscoverAI(GOOD)
    assert run_discover(runtime_conn, fake, name, cap_usd=Decimal("25")) == "budget"  # $0.01 left, a discovery can cost $0.08+
    assert fake.discover_calls() == []
    with runtime_conn.cursor() as cur:
        cur.execute("UPDATE pages SET ai_reserved_usd = 0 WHERE id = %s", (page_id,))
    assert isinstance(run_discover(runtime_conn, fake, name, cap_usd=Decimal("25")), dict)  # control: free again, so it runs


def test_discover_says_when_the_answer_was_not_usable(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    assert run_discover(runtime_conn, FakeDiscoverAI("no idea"), name) == "unusable"


@pytest.mark.parametrize("tokens,factor", [(100_000, 1), (100_001, 5)])
def test_the_long_prompt_rate_is_on_by_default_and_can_be_turned_off(tokens, factor):
    from crawler.ai import cost
    plain = cost(HAIKU, input_tokens=tokens, output_tokens=1000, check_long=False)
    default = cost(HAIKU, input_tokens=tokens, output_tokens=1000)
    assert Decimal(str(default)) == Decimal(str(plain)) * factor


# ---------------------------------------------------------------- sitemap folder rule

@pytest.fixture
def w(runtime_conn):
    world = World(runtime_conn)
    world.approve("ruud.test")
    world.site.route("/p", b"<html><body>hello</body></html>", headers={"Content-Type": "text/html"})
    assert world.fetcher().fetch(world.url("ruud.test", "/p"), world.brand_id).outcome == "fetched"
    world.site.requests.clear()
    with runtime_conn.cursor() as cur:
        cur.execute("UPDATE hosts SET robots_body = %s, robots_status = 200, robots_fetched_at = %s WHERE host = 'ruud.test'",
                    (f"User-agent: *\nDisallow:\nSitemap: {world.url('ruud.test', '/sitemap.xml')}\n", world.clock.now()))
    try:
        yield world
    finally:
        world.site.stop()


def added(w):
    from crawler.sitemaps import read_sitemaps
    read_sitemaps(w.conn, w.fetcher(), w.brand)
    return [r[0] for r in q(w.conn, "SELECT url FROM start_urls WHERE brand_id = %s AND found_by = 'sitemap' ORDER BY id", (w.brand_id,))]


def test_a_start_url_without_a_trailing_slash_uses_its_folder(w):
    from crawler.brands import add_start_url
    add_start_url(w.conn, w.brand, w.url("ruud.test", "/products/hvac/air_conditioners"))  # folder is /products/hvac/
    inside = w.url("ruud.test", "/products/hvac/furnaces/g1")
    outside = w.url("ruud.test", "/products/plumbing/x")
    w.site.route("/sitemap.xml", urlset(inside, outside), headers=XML)
    assert added(w) == [inside]


def test_a_start_url_in_the_root_folder_gets_no_sitemap_expansion(w):
    from crawler.brands import add_start_url
    add_start_url(w.conn, w.brand, w.url("ruud.test", "/home"))  # folder is /
    w.site.route("/sitemap.xml", urlset(w.url("ruud.test", "/products/x"), w.url("ruud.test", "/about")), headers=XML)
    assert added(w) == []


def test_a_sitemap_url_on_another_host_does_not_match_a_start_urls_folder(w):
    from crawler.brands import add_start_url
    w.approve("other.test")
    add_start_url(w.conn, w.brand, w.url("ruud.test", "/products/"))
    w.site.route("/sitemap.xml", urlset(w.url("other.test", "/products/x"), w.url("ruud.test", "/products/y")), headers=XML)
    assert added(w) == [w.url("ruud.test", "/products/y")]
