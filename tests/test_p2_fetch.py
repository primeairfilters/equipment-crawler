"""P2: the polite, safe fetcher, against a fake web server in this process.

No real network and no real sleeping: names like ruud.test are mapped to 127.0.0.1 by an
injected resolver, loopback is allowed ONLY by an injected address policy (the tests that
prove the real policy leave it out), and time is a fake clock that only moves when the fetcher
asks to sleep. Database rows are written through runtime_conn and rolled back.
"""
import hashlib
import uuid

import pytest

from fakesite import FakeClock, FakeSite, gzip_bytes, gzip_zeros

UA = "TracforceEquipmentBot/1.0 (+mailto:support+crawler@tracforce.ai)"
HTML = b"<html><head><title>RA15AZ</title></head><body>hello</body></html>"


class World:
    def __init__(self, conn):
        from crawler.brands import add_brand, approve_host
        self.conn = conn
        self.clock = FakeClock()
        self.site = FakeSite(self.clock)
        self.site.start()
        self.brand = f"[T] Ruud {uuid.uuid4().hex[:8]}"
        self.brand_id = add_brand(conn, self.brand)
        self.approve = lambda host: approve_host(conn, host, brand=self.brand)
        self.resolved = []
        self.special_ips = {}  # host -> ip, everything else resolves to 127.0.0.1

    def resolver(self, host):
        self.resolved.append(host)
        if host in self.special_ips:
            return [self.special_ips[host]]
        if host.replace(".", "").isdigit():
            return [host]
        return ["127.0.0.1"]

    def fetcher(self, allow_loopback=True, resolver=None):
        from crawler.fetch import Fetcher
        from crawler.netpolicy import is_public_ip
        kwargs = dict(resolver=resolver or self.resolver, now=self.clock.now, sleep=self.clock.sleep)
        if allow_loopback:
            kwargs["ip_allowed"] = lambda ip: ip == "127.0.0.1" or is_public_ip(ip)
        return Fetcher(self.conn, **kwargs)

    def url(self, host, path):
        return self.site.url(host, path)

    def one(self, sql, args=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()


@pytest.fixture
def world(runtime_conn):
    w = World(runtime_conn)
    try:
        yield w
    finally:
        w.site.stop()


@pytest.fixture
def ruud(world):
    """An approved host with a plain page at /p and no robots.txt (404, which means allowed)."""
    world.approve("ruud.test")
    world.site.route("/p", HTML, headers={"Content-Type": "text/html; charset=utf-8"})
    return world


def get(world, path="/p", host="ruud.test", fetcher=None):
    f = fetcher or world.fetcher()
    return f.fetch(world.url(host, path), world.brand_id)


# ---------------------------------------------------------------- the happy path and the page row

def test_fetch_returns_the_page_and_records_it(ruud):
    result = get(ruud)
    assert result.outcome == "fetched"
    assert result.http_status == 200
    assert result.body == HTML
    assert result.url == ruud.url("ruud.test", "/p")
    rows = ruud.one("SELECT p.http_status, p.content_type, p.content_sha, p.is_pdf, p.fetched_at, p.depth, h.host "
                    "FROM pages p JOIN hosts h ON h.id = p.host_id WHERE p.brand_id = %s", (ruud.brand_id,))
    assert len(rows) == 1
    status, ctype, sha, is_pdf, fetched_at, depth, host = rows[0]
    assert (status, is_pdf, depth, host) == (200, 0, 0, "ruud.test")
    assert ctype.startswith("text/html")
    assert sha == hashlib.sha256(HTML).hexdigest()
    assert fetched_at is not None


def test_every_request_carries_the_identified_user_agent(ruud):
    get(ruud)
    assert len(ruud.site.requests) >= 2  # robots.txt and the page
    assert {r["headers"].get("user-agent") for r in ruud.site.requests} == {UA}


def test_a_redirect_inside_scope_is_followed(ruud):
    ruud.site.route("/old", status=301, headers={"Location": ruud.url("ruud.test", "/p")})
    result = get(ruud, "/old")
    assert result.outcome == "fetched"
    assert result.url == ruud.url("ruud.test", "/p")
    assert result.body == HTML


def test_too_many_redirects_fail(ruud):
    ruud.site.route("/loop", status=302, headers={"Location": ruud.url("ruud.test", "/loop")})
    result = get(ruud, "/loop")
    assert (result.outcome, result.reason) == ("failed", "redirects")
    assert len(ruud.site.hits("/loop")) <= 6


@pytest.mark.parametrize("url", ["ftp://ruud.test/x", "file:///etc/passwd", "gopher://ruud.test/x"])
def test_only_http_and_https_are_fetched(ruud, url):
    result = ruud.fetcher().fetch(url, ruud.brand_id)
    assert (result.outcome, result.reason) == ("refused", "scheme")
    assert ruud.site.requests == []


# ---------------------------------------------------------------- scope: approved hosts only

def test_a_host_still_waiting_for_ok_is_not_fetched_and_ok_unlocks_it(world):
    from crawler.brands import approve_host
    with world.conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, 'ruud.test', 'waiting', 'ai')",
                    (world.brand_id,))
    world.site.route("/p", HTML, headers={"Content-Type": "text/html"})
    refused = get(world)
    assert (refused.outcome, refused.reason) == ("refused", "scope")
    assert world.site.requests == []  # nothing, not even robots.txt

    approve_host(world.conn, "ruud.test")
    allowed = get(world)  # control: same URL, now approved, does reach the server
    assert allowed.outcome == "fetched"
    assert world.site.hits("/p")


def test_a_host_nobody_added_is_not_fetched(world):
    world.site.route("/p", HTML, headers={"Content-Type": "text/html"})
    result = get(world)
    assert (result.outcome, result.reason) == ("refused", "scope")
    assert world.site.requests == []


def test_a_stopped_host_is_not_fetched(ruud):
    from crawler.brands import stop
    assert get(ruud).outcome == "fetched"  # control
    stop(ruud.conn, "ruud.test")
    ruud.site.requests.clear()
    result = get(ruud)
    assert (result.outcome, result.reason) == ("refused", "scope")
    assert ruud.site.requests == []


def test_a_true_subdomain_of_an_approved_host_is_fetched(ruud):
    ruud.site.route("/p", HTML, headers={"Content-Type": "text/html"})
    result = get(ruud, host="shop.ruud.test")
    assert result.outcome == "fetched"
    assert ruud.site.hits("/p", host="shop.ruud.test")


@pytest.mark.parametrize("host", ["evilruud.test", "xruud.test", "ruud.test.evil.test"])
def test_look_alike_hosts_are_refused(ruud, host):
    result = get(ruud, host=host)
    assert (result.outcome, result.reason) == ("refused", "scope")
    assert ruud.site.requests == []  # even though the resolver would have sent it to our server


def test_userinfo_trick_is_refused(ruud):
    url = f"http://ruud.test@evilruud.test:{ruud.site.port}/p"
    result = ruud.fetcher().fetch(url, ruud.brand_id)
    assert (result.outcome, result.reason) == ("refused", "scope")
    assert ruud.site.requests == []


def test_a_redirect_to_an_unapproved_host_is_refused_before_it_is_requested(ruud):
    ruud.site.route("/away", status=302, headers={"Location": ruud.url("evilruud.test", "/p")})
    result = get(ruud, "/away")
    assert (result.outcome, result.reason) == ("refused", "scope")
    assert ruud.site.hits("/away")  # the first hop did run
    assert ruud.site.hits(host="evilruud.test") == []


def test_ahri_is_never_fetched_even_if_the_database_says_approved(ruud):
    with ruud.conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) "
                    "VALUES (%s, 'ahridirectory.org', 'approved', 'user')", (ruud.brand_id,))
    ruud.site.route("/p", HTML, headers={"Content-Type": "text/html"})
    result = get(ruud, host="ahridirectory.org")
    assert (result.outcome, result.reason) == ("refused", "refused_host")
    assert ruud.site.hits(host="ahridirectory.org") == []
    assert get(ruud).outcome == "fetched"  # control: an ordinary approved host still works


# ---------------------------------------------------------------- SSRF

def test_the_real_policy_refuses_a_host_that_resolves_to_loopback(world):
    world.approve("ruud.test")
    world.site.route("/p", HTML, headers={"Content-Type": "text/html"})
    result = world.fetcher(allow_loopback=False).fetch(world.url("ruud.test", "/p"), world.brand_id)
    assert (result.outcome, result.reason) == ("refused", "ip")
    assert "ruud.test" in world.resolved  # the lookup did happen
    assert world.site.requests == []      # and nothing was connected to


def test_the_real_policy_refuses_a_literal_loopback_address(world):
    with world.conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) "
                    "VALUES (%s, '127.0.0.1', 'approved', 'user')", (world.brand_id,))
    result = world.fetcher(allow_loopback=False).fetch(f"http://127.0.0.1:{world.site.port}/p", world.brand_id)
    assert (result.outcome, result.reason) == ("refused", "ip")
    assert world.site.requests == []


def test_a_redirect_to_the_cloud_metadata_address_is_refused(ruud):
    with ruud.conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) "
                    "VALUES (%s, '169.254.169.254', 'approved', 'user')", (ruud.brand_id,))
    ruud.site.route("/meta", status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    result = get(ruud, "/meta")
    assert (result.outcome, result.reason) == ("refused", "ip")
    assert ruud.site.hits("/meta")  # the first hop ran and was served
    assert all(r["host"] != "169.254.169.254" for r in ruud.site.requests)


def test_a_redirect_to_an_in_scope_name_that_resolves_to_metadata_is_refused(ruud):
    ruud.special_ips["metadata.ruud.test"] = "169.254.169.254"
    ruud.site.route("/meta", status=302, headers={"Location": ruud.url("metadata.ruud.test", "/latest")})
    result = get(ruud, "/meta")
    assert (result.outcome, result.reason) == ("refused", "ip")
    assert "metadata.ruud.test" in ruud.resolved
    assert ruud.site.hits("/latest") == []


def test_each_request_is_resolved_once_and_connected_to_that_address(ruud):
    calls_at = []  # how many requests the server had seen at each lookup

    def rebinding_resolver(host):
        # The first lookup before each request answers with the allowed address. A second
        # lookup before the same request (check, then connect) is answered with the metadata
        # address, which is where a fetcher that resolves twice would be sent.
        n = len(ruud.site.requests)
        repeat = n in calls_at
        calls_at.append(n)
        return ["169.254.169.254"] if repeat else ["127.0.0.1"]

    ruud.site.route("/robots.txt", b"User-agent: *\nDisallow: /nope/\n", headers={"Content-Type": "text/plain"})
    f = ruud.fetcher(resolver=rebinding_resolver)
    first = f.fetch(ruud.url("ruud.test", "/p"), ruud.brand_id)
    assert first.outcome == "fetched"
    assert len(ruud.site.requests) == 2  # robots.txt and the page, so the path ran
    assert sorted(calls_at) == [0, 1]    # exactly one lookup before each request


# ---------------------------------------------------------------- robots.txt (RFC 9309)

def test_robots_disallow_blocks_the_page_and_other_pages_still_load(ruud):
    ruud.site.route("/robots.txt", b"User-agent: *\nDisallow: /private/\n", headers={"Content-Type": "text/plain"})
    ruud.site.route("/private/x", HTML, headers={"Content-Type": "text/html"})
    blocked = get(ruud, "/private/x")
    assert (blocked.outcome, blocked.reason) == ("refused", "robots")
    assert ruud.site.hits("/robots.txt")      # robots was read
    assert ruud.site.hits("/private/x") == []  # and the page was not requested
    assert get(ruud, "/p").outcome == "fetched"  # control: the rules do not block everything


def test_robots_rule_for_our_own_bot_name_is_obeyed(ruud):
    ruud.site.route("/robots.txt", b"User-agent: TracforceEquipmentBot\nDisallow: /\n",
                    headers={"Content-Type": "text/plain"})
    result = get(ruud, "/p")
    assert (result.outcome, result.reason) == ("refused", "robots")
    assert ruud.site.hits("/robots.txt")
    assert ruud.site.hits("/p") == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_robots_4xx_means_allowed(ruud, status):
    ruud.site.route("/robots.txt", b"Disallow: /", status=status)
    result = get(ruud)
    assert result.outcome == "fetched"
    assert ruud.site.hits("/robots.txt")


@pytest.mark.parametrize("status", [500, 502, 503])
def test_robots_5xx_means_blocked(ruud, status):
    ruud.site.route("/robots.txt", b"oops", status=status)
    result = get(ruud)
    assert (result.outcome, result.reason) == ("refused", "robots")
    assert ruud.site.hits("/robots.txt")
    assert ruud.site.hits("/p") == []


def test_robots_unreachable_means_blocked(ruud):
    # Nothing listens on the port any more, so robots.txt cannot be reached.
    port_url = ruud.url("ruud.test", "/p")
    ruud.site.stop()
    result = ruud.fetcher().fetch(port_url, ruud.brand_id)
    assert (result.outcome, result.reason) == ("refused", "robots")
    assert not result.body


def test_robots_is_cached_for_24_hours(ruud):
    ruud.site.route("/robots.txt", b"User-agent: *\nDisallow:\n", headers={"Content-Type": "text/plain"})
    ruud.site.route("/q", HTML, headers={"Content-Type": "text/html"})
    f = ruud.fetcher()
    f.fetch(ruud.url("ruud.test", "/p"), ruud.brand_id)
    f.fetch(ruud.url("ruud.test", "/q"), ruud.brand_id)
    assert len(ruud.site.hits("/robots.txt")) == 1
    ruud.clock.advance(23 * 3600)
    f.fetch(ruud.url("ruud.test", "/p"), ruud.brand_id)
    assert len(ruud.site.hits("/robots.txt")) == 1
    ruud.clock.advance(2 * 3600)
    f.fetch(ruud.url("ruud.test", "/p"), ruud.brand_id)
    assert len(ruud.site.hits("/robots.txt")) == 2


def test_robots_cache_survives_a_new_fetcher(ruud):
    ruud.site.route("/robots.txt", b"User-agent: *\nDisallow:\n", headers={"Content-Type": "text/plain"})
    get(ruud)
    get(ruud, fetcher=ruud.fetcher())  # a brand new Fetcher on the same database
    assert len(ruud.site.hits("/robots.txt")) == 1


# ---------------------------------------------------------------- politeness: the gap

def host_times(world, host):
    return [r["t"] for r in world.site.requests if r["host"] == host]


def test_at_least_ten_seconds_between_requests_to_one_host(ruud):
    ruud.site.route("/q", HTML, headers={"Content-Type": "text/html"})
    get(ruud, "/p")
    get(ruud, "/q")
    get(ruud, "/p")
    times = host_times(ruud, "ruud.test")
    assert len(times) >= 3
    for a, b in zip(times, times[1:]):
        assert (b - a).total_seconds() >= 10
    assert sum(ruud.clock.sleeps) > 0  # it waited by asking the injected sleep, not by luck


def test_the_gap_counts_time_already_passed(ruud):
    ruud.site.route("/q", HTML, headers={"Content-Type": "text/html"})
    get(ruud, "/p")
    before = len(ruud.site.requests)
    ruud.clock.advance(60)
    slept_before = sum(ruud.clock.sleeps)
    get(ruud, "/q")
    assert len(ruud.site.requests) == before + 1
    assert sum(ruud.clock.sleeps) == slept_before  # a minute passed already, so no extra wait


def test_the_gap_holds_across_a_new_fetcher(ruud):
    get(ruud, "/p")
    last = host_times(ruud, "ruud.test")[-1]
    get(ruud, "/p", fetcher=ruud.fetcher())
    assert (host_times(ruud, "ruud.test")[-1] - last).total_seconds() >= 10


def test_different_hosts_do_not_wait_for_each_other(ruud):
    ruud.approve("other.test")
    get(ruud, "/p")
    last_ruud = host_times(ruud, "ruud.test")[-1]
    get(ruud, "/p", host="other.test")
    first_other = host_times(ruud, "other.test")[0]
    assert (first_other - last_ruud).total_seconds() < 10


def test_429_with_retry_after_waits_at_least_that_long(ruud):
    ruud.site.route("/busy", b"slow down", status=429, headers={"Retry-After": "120"})
    result = get(ruud, "/busy")
    assert (result.outcome, result.reason) == ("failed", "http_429")
    get(ruud, "/p")
    busy = ruud.site.hits("/busy")[0]["t"]
    after = ruud.site.hits("/p")[0]["t"]
    assert (after - busy).total_seconds() >= 120


def test_503_without_retry_after_doubles_the_gap(ruud):
    ruud.site.route("/down", b"later", status=503)
    result = get(ruud, "/down")
    assert (result.outcome, result.reason) == ("failed", "http_503")
    get(ruud, "/p")
    down = ruud.site.hits("/down")[0]["t"]
    after = ruud.site.hits("/p")[0]["t"]
    assert (after - down).total_seconds() >= 20


def test_five_failures_rest_the_host_for_24_hours(ruud):
    ruud.site.route("/bad", b"boom", status=500)
    for _ in range(5):
        assert get(ruud, "/bad").outcome == "failed"
    row = ruud.one("SELECT failure_count, rest_until FROM hosts WHERE host = 'ruud.test'")[0]
    assert row[0] == 5
    assert row[1] is not None and row[1] > ruud.clock.now()
    assert len(ruud.site.hits("/bad")) == 5

    resting = get(ruud, "/p")
    assert (resting.outcome, resting.reason) == ("refused", "rest")
    assert ruud.site.hits("/p") == []  # no request at all while resting

    ruud.clock.advance(25 * 3600)
    assert get(ruud, "/p").outcome == "fetched"  # control: it picks up again after the rest


def test_a_404_page_is_recorded_as_failed_but_does_not_rest_the_host(ruud):
    result = get(ruud, "/missing")
    assert (result.outcome, result.reason) == ("failed", "http_404")
    assert ruud.one("SELECT http_status FROM pages WHERE brand_id = %s", (ruud.brand_id,)) == ((404,),)
    for _ in range(6):
        get(ruud, "/missing")
    assert get(ruud, "/p").outcome == "fetched"


# ---------------------------------------------------------------- conditional requests (304)

def test_etag_is_sent_back_and_a_304_changes_nothing(ruud):
    def page(rec):
        if rec["headers"].get("if-none-match") == '"v1"':
            return 304, {"ETag": '"v1"'}, b""
        return 200, {"ETag": '"v1"', "Content-Type": "text/html"}, HTML

    ruud.site.route("/e", page)
    first = get(ruud, "/e")
    assert first.outcome == "fetched"
    row = ruud.one("SELECT etag, content_sha FROM pages WHERE brand_id = %s", (ruud.brand_id,))[0]
    assert row == ('"v1"', hashlib.sha256(HTML).hexdigest())

    second = get(ruud, "/e")
    assert second.outcome == "not_modified"
    assert not second.body
    assert ruud.site.hits("/e")[-1]["headers"].get("if-none-match") == '"v1"'
    assert ruud.one("SELECT COUNT(*), MAX(content_sha), MAX(etag) FROM pages WHERE brand_id = %s",
                    (ruud.brand_id,))[0] == (1, row[1], '"v1"')


def test_last_modified_is_sent_back_as_if_modified_since(ruud):
    stamp = "Wed, 21 Oct 2026 07:28:00 GMT"

    def page(rec):
        if rec["headers"].get("if-modified-since") == stamp:
            return 304, {}, b""
        return 200, {"Last-Modified": stamp, "Content-Type": "text/html"}, HTML

    ruud.site.route("/m", page)
    assert get(ruud, "/m").outcome == "fetched"
    assert get(ruud, "/m").outcome == "not_modified"
    assert ruud.site.hits("/m")[-1]["headers"].get("if-modified-since") == stamp


def test_a_server_that_ignores_conditionals_is_compared_by_hash(ruud):
    state = {"body": HTML}
    ruud.site.route("/h", lambda rec: (200, {"Content-Type": "text/html"}, state["body"]))
    assert get(ruud, "/h").outcome == "fetched"
    assert get(ruud, "/h").outcome == "not_modified"  # same bytes: not new

    state["body"] = HTML + b"<!-- changed -->"
    changed = get(ruud, "/h")
    assert changed.outcome == "fetched"
    assert changed.body == state["body"]
    assert ruud.one("SELECT content_sha FROM pages WHERE brand_id = %s", (ruud.brand_id,)) == (
        (hashlib.sha256(state["body"]).hexdigest(),),)


# ---------------------------------------------------------------- size caps (decoded bytes) and types

def test_a_page_exactly_at_the_html_cap_is_accepted_and_one_byte_more_is_not(ruud):
    from crawler.fetch import MAX_HTML_BYTES
    ruud.site.route("/ok", b"a" * MAX_HTML_BYTES, headers={"Content-Type": "text/html"})
    ruud.site.route("/big", b"a" * (MAX_HTML_BYTES + 1), headers={"Content-Type": "text/html"})
    assert get(ruud, "/ok").outcome == "fetched"
    result = get(ruud, "/big")
    assert (result.outcome, result.reason) == ("failed", "too_big")
    assert not result.body


def test_a_small_gzip_page_is_decoded(ruud):
    ruud.site.route("/z", gzip_bytes(HTML), headers={"Content-Type": "text/html", "Content-Encoding": "gzip"})
    result = get(ruud, "/z")
    assert result.outcome == "fetched"
    assert result.body == HTML
    assert ruud.one("SELECT content_sha FROM pages WHERE brand_id = %s", (ruud.brand_id,)) == (
        (hashlib.sha256(HTML).hexdigest(),),)


def test_a_gzip_bomb_html_is_refused_by_decoded_size(ruud):
    from crawler.fetch import MAX_HTML_BYTES
    bomb = gzip_zeros(MAX_HTML_BYTES + 1024 * 1024)
    assert len(bomb) < 1024 * 1024  # control: tiny on the wire, so a wire-size cap would let it through
    ruud.site.route("/bomb", bomb, headers={"Content-Type": "text/html", "Content-Encoding": "gzip"})
    result = get(ruud, "/bomb")
    assert (result.outcome, result.reason) == ("failed", "too_big")
    assert not result.body


def test_a_gzip_bomb_pdf_is_refused_by_decoded_size(ruud):
    from crawler.fetch import MAX_PDF_BYTES
    bomb = gzip_zeros(MAX_PDF_BYTES + 1024 * 1024, prefix=b"%PDF-1.7\n")
    assert len(bomb) < 5 * 1024 * 1024
    ruud.site.route("/bomb.pdf", bomb, headers={"Content-Type": "application/pdf", "Content-Encoding": "gzip"})
    result = get(ruud, "/bomb.pdf")
    assert (result.outcome, result.reason) == ("failed", "too_big")
    assert not result.body


def test_a_real_pdf_is_fetched_and_marked_as_pdf(ruud):
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    ruud.site.route("/doc.pdf", pdf, headers={"Content-Type": "application/pdf"})
    result = get(ruud, "/doc.pdf")
    assert result.outcome == "fetched"
    assert result.body == pdf
    assert ruud.one("SELECT is_pdf FROM pages WHERE brand_id = %s", (ruud.brand_id,)) == ((1,),)


def test_a_pdf_content_type_without_the_pdf_signature_is_rejected(ruud):
    ruud.site.route("/fake.pdf", HTML, headers={"Content-Type": "application/pdf"})
    result = get(ruud, "/fake.pdf")
    assert (result.outcome, result.reason) == ("failed", "not_pdf")
    assert not result.body


def test_other_content_types_are_rejected(ruud):
    ruud.site.route("/img.png", b"\x89PNG\r\n", headers={"Content-Type": "image/png"})
    result = get(ruud, "/img.png")
    assert (result.outcome, result.reason) == ("failed", "bad_content_type")
