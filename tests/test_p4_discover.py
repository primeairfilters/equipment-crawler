"""P4: brand discovery with a FAKE web-search client. No network, no key.

discover(conn, client, brand_name, *, now=None, cap_usd=None) never commits. The client has
supports_web_search(model) and discover(model, brand, max_uses, max_tokens), see FakeDiscoverAI.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fakeai import FakeDiscoverAI
from p3support import new_brand, q

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
T0 = datetime(2031, 5, 10, 9, 0)
EVIDENCE = "https://www.ruud.com/about/"


def found(domains=(), docs=(), starts=()):
    def entry(x):
        return x if isinstance(x, dict) else {"host": x, "evidence_url": EVIDENCE}

    def start(x):
        return x if isinstance(x, dict) else {"url": x, "evidence_url": EVIDENCE}

    return {"domains": [entry(d) for d in domains], "document_hosts": [entry(d) for d in docs],
            "start_urls": [start(s) for s in starts]}


GOOD = found(domains=["ruud.com"], docs=["files.myrheem.com"], starts=["https://www.ruud.com/products/hvac/air_conditioners/"])


def run_discover(conn, fake, name, **kw):
    from crawler.discover import discover
    return discover(conn, fake, name, now=kw.pop("now", T0), **kw)


def domains(conn, brand_id):
    return q(conn, "SELECT host, status, found_by, evidence_url FROM brand_domains WHERE brand_id = %s ORDER BY host", (brand_id,))


def start_urls(conn, brand_id):
    return q(conn, "SELECT url, found_by, status FROM start_urls WHERE brand_id = %s ORDER BY url", (brand_id,))


# ---------------------------------------------------------------- the one call and what it stores

def test_discovery_makes_one_web_search_call_with_the_agreed_limits(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(GOOD)
    run_discover(runtime_conn, fake, name)
    calls = fake.discover_calls()
    assert len(calls) == 1
    _, model, brand, max_uses, max_tokens = calls[0]
    assert model == HAIKU and brand == name
    assert max_uses == 8
    assert isinstance(max_tokens, int) and 0 < max_tokens


def test_found_domains_and_document_hosts_wait_for_ok_with_their_evidence(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    run_discover(runtime_conn, FakeDiscoverAI(GOOD), name)
    assert domains(runtime_conn, brand_id) == (
        ("files.myrheem.com", "waiting", "ai", EVIDENCE),
        ("ruud.com", "waiting", "ai", EVIDENCE),
    )


def test_found_start_urls_are_stored_as_discover(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    run_discover(runtime_conn, FakeDiscoverAI(GOOD), name)
    assert start_urls(runtime_conn, brand_id) == (
        ("https://www.ruud.com/products/hvac/air_conditioners/", "discover", "active"),)


def test_host_names_are_normalised(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    run_discover(runtime_conn, FakeDiscoverAI(found(domains=["WWW.Ruud.COM.", "BÜCHER.example"])), name)
    assert [r[0] for r in domains(runtime_conn, brand_id)] == ["www.ruud.com", "xn--bcher-kva.example"]


def test_an_approved_or_stopped_host_is_never_changed_by_discovery(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, 'ruud.com', 'approved', 'user')",
                    (brand_id,))
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, 'files.myrheem.com', 'stopped', 'user')",
                    (brand_id,))
    run_discover(runtime_conn, FakeDiscoverAI(GOOD), name)
    assert domains(runtime_conn, brand_id) == (
        ("files.myrheem.com", "stopped", "user", None),
        ("ruud.com", "approved", "user", None),
    )


def test_a_start_url_the_user_added_stays_the_users(runtime_conn):
    from crawler.brands import add_start_url
    name, brand_id = new_brand(runtime_conn)
    url = "https://www.ruud.com/products/hvac/air_conditioners/"
    add_start_url(runtime_conn, name, url)
    run_discover(runtime_conn, FakeDiscoverAI(GOOD), name)
    assert start_urls(runtime_conn, brand_id) == ((url, "user", "active"),)


# ---------------------------------------------------------------- what is refused or dropped

AHRI = ["ahridirectory.org", "www.ahridirectory.org", "AHRIDIRECTORY.ORG.", "ahrinet.org", "www.ahrinet.org",
        "deep.sub.ahrinet.org"]


def test_ahri_hosts_and_start_urls_are_never_stored(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    out = found(domains=["ruud.com", *AHRI], docs=["files.myrheem.com", "www.ahridirectory.org"],
                starts=["https://www.ruud.com/products/", "https://www.ahridirectory.org/search/", "http://ahrinet.org/x"])
    run_discover(runtime_conn, FakeDiscoverAI(out), name)
    assert [r[0] for r in domains(runtime_conn, brand_id)] == ["files.myrheem.com", "ruud.com"]  # the good ones went in
    assert [r[0] for r in start_urls(runtime_conn, brand_id)] == ["https://www.ruud.com/products/"]


BAD_HOSTS = [None, 42, "", "   ", "ruud.com/path", "http://ruud.com", "ruud.com:8080", "user@ruud.com", "localhost",
             "127.0.0.1", "169.254.169.254", "10.0.0.5", "[::1]", "a..b.com", "bad host.com"]
BAD_URLS = [None, 5, "", "ftp://ruud.com/x", "javascript:alert(1)", "file:///etc/passwd", "ruud.com/x",
            "http://" + "a" * 3000 + ".com/"]


def test_malformed_hosts_and_urls_from_the_ai_are_dropped_without_stopping_the_rest(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    out = {"domains": [{"host": h, "evidence_url": EVIDENCE} for h in BAD_HOSTS] + [{"host": "ruud.com", "evidence_url": EVIDENCE}],
           "document_hosts": [{"host": h, "evidence_url": EVIDENCE} for h in BAD_HOSTS[:6]],
           "start_urls": [{"url": u, "evidence_url": EVIDENCE} for u in BAD_URLS] + [{"url": "https://www.ruud.com/p/", "evidence_url": EVIDENCE}]}
    run_discover(runtime_conn, FakeDiscoverAI(out), name)
    assert [r[0] for r in domains(runtime_conn, brand_id)] == ["ruud.com"]
    assert [r[0] for r in start_urls(runtime_conn, brand_id)] == ["https://www.ruud.com/p/"]


@pytest.mark.parametrize("out", [{"domains": "ruud.com"}, {"domains": [None, "ruud.com", 7]}, {}, {"unrelated": 1}, {"domains": [{}]}])
def test_an_answer_of_the_wrong_shape_does_not_crash_and_stores_nothing_bad(runtime_conn, out):
    name, brand_id = new_brand(runtime_conn)
    run_discover(runtime_conn, FakeDiscoverAI(out), name)
    assert len(domains(runtime_conn, brand_id)) == 0
    assert len(start_urls(runtime_conn, brand_id)) == 0


def test_nothing_that_was_found_is_fetched_before_ok_and_ok_unlocks_it(runtime_conn):
    from crawler.brands import approve_host
    from crawler.crawl import run
    from test_p2_fetch import World
    w = World(runtime_conn)
    try:
        html = {"Content-Type": "text/html"}
        w.site.route("/start", b"<html><body>hello</body></html>", headers=html)
        found_out = found(domains=["ruud.test"], starts=[w.url("ruud.test", "/start")])
        run_discover(runtime_conn, FakeDiscoverAI(found_out), w.brand)
        assert domains(runtime_conn, w.brand_id)[0][:3] == ("ruud.test", "waiting", "ai")

        run(runtime_conn, w.fetcher(), brand=w.brand)
        assert w.site.requests == []  # not even robots.txt

        approve_host(runtime_conn, "ruud.test")
        run(runtime_conn, w.fetcher(), brand=w.brand)
        assert [r["path"] for r in w.site.page_hits()] == ["/start"]  # control: after ok the same start URL is fetched
    finally:
        w.site.stop()


# ---------------------------------------------------------------- the call is logged

def test_the_call_is_logged_with_searches_and_their_cost(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(GOOD, input_tokens=3000, output_tokens=500, searches=5)
    run_discover(runtime_conn, fake, name)
    rows = q(runtime_conn, "SELECT purpose, brand_id, page_id, model, input_tokens, output_tokens, searches, is_batch, "
                           "rejected, cost_usd FROM ai_calls WHERE brand_id = %s", (brand_id,))
    assert len(rows) == 1
    purpose, b, page, model, tin, tout, searches, is_batch, rejected, cost = rows[0]
    assert (purpose, b, page, model, tin, tout, searches, is_batch, rejected) == ("discover", brand_id, None, HAIKU, 3000, 500, 5, 0, 0)
    assert Decimal(str(cost)) == Decimal("0.05055")  # 3000 in at $0.10 + 500 out at $0.50 per MTok + 5 searches at $0.01


@pytest.mark.parametrize("truncated,output", [(False, "I could not find anything useful."), (True, None)])
def test_an_unusable_answer_is_logged_as_rejected_stores_nothing_and_can_be_retried(runtime_conn, truncated, output):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(output if output is not None else "{\"domains\": [", truncated=truncated)
    run_discover(runtime_conn, fake, name)
    assert len(fake.discover_calls()) == 1  # it was paid for
    row = q(runtime_conn, "SELECT rejected, cost_usd FROM ai_calls WHERE brand_id = %s", (brand_id,))
    assert len(row) == 1 and row[0][0] == 1 and row[0][1] > 0
    assert len(domains(runtime_conn, brand_id)) == 0 and len(start_urls(runtime_conn, brand_id)) == 0
    assert q(runtime_conn, "SELECT discovered_at FROM brands WHERE id = %s", (brand_id,)) == ((None,),)  # so it can be tried again


# ---------------------------------------------------------------- the monthly cap

def test_a_cap_below_the_worst_case_of_one_discovery_makes_no_call(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(GOOD)
    run_discover(runtime_conn, fake, name, cap_usd=Decimal("0.05"))  # 8 searches alone could cost $0.08
    assert fake.discover_calls() == []
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE brand_id = %s", (brand_id,))[0][0] == 0
    assert q(runtime_conn, "SELECT discovered_at FROM brands WHERE id = %s", (brand_id,)) == ((None,),)
    run_discover(runtime_conn, fake, name, cap_usd=Decimal("25"))  # control: with room, the same brand is discovered
    assert len(fake.discover_calls()) == 1


def test_discovery_pauses_when_the_month_is_nearly_spent_and_resumes_on_the_first(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO ai_calls (purpose, model, cost_usd, created_at) VALUES ('test', %s, %s, %s)",
                    (HAIKU, "24.95", "2031-05-02 10:00:00"))
    fake = FakeDiscoverAI(GOOD)
    run_discover(runtime_conn, fake, name, cap_usd=Decimal("25"), now=datetime(2031, 5, 20, 9, 0))
    assert fake.discover_calls() == []
    run_discover(runtime_conn, fake, name, cap_usd=Decimal("25"), now=datetime(2031, 6, 1, 0, 5))
    assert len(fake.discover_calls()) == 1


# ---------------------------------------------------------------- every 180 days

def test_discovery_runs_again_only_after_180_days(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(GOOD)
    run_discover(runtime_conn, fake, name, now=T0)
    assert len(fake.discover_calls()) == 1
    assert q(runtime_conn, "SELECT discovered_at FROM brands WHERE id = %s", (brand_id,)) == ((T0,),)

    calls = list(fake.calls)
    run_discover(runtime_conn, fake, name, now=T0 + timedelta(days=179))
    assert fake.calls == calls  # no web-search call and not even a capability check
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE brand_id = %s", (brand_id,))[0][0] == 1

    run_discover(runtime_conn, fake, name, now=T0 + timedelta(days=181))
    assert len(fake.discover_calls()) == 2


# ---------------------------------------------------------------- Q7: which model

def test_haiku_is_used_when_it_can_search_the_web(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(GOOD)
    run_discover(runtime_conn, fake, name)
    assert ("supports_web_search", HAIKU) in fake.calls
    assert fake.discover_calls()[0][1] == HAIKU


def test_sonnet_is_used_when_haiku_cannot_search_the_web(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(GOOD, web_search_models=(SONNET,), input_tokens=3000, output_tokens=500, searches=5)
    run_discover(runtime_conn, fake, name)
    assert ("supports_web_search", HAIKU) in fake.calls
    assert [c[1] for c in fake.discover_calls()] == [SONNET]
    row = q(runtime_conn, "SELECT model, cost_usd FROM ai_calls WHERE brand_id = %s", (brand_id,))
    assert row[0][0] == SONNET
    assert Decimal(str(row[0][1])) == Decimal("0.061")  # 3000 in at $2 + 500 out at $10 per MTok + 5 searches at $0.01
