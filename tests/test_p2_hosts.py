"""P2: host names, scope, the AHRI refusal, and the brand/host commands (add, ok, stop, status).

The command functions never commit; the caller (the CLI) does. That is what lets these tests
run inside the runtime_conn transaction and roll back. Only the two CLI tests commit, using
brand and host names unique to the run.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from support import clean_env, db_name

ROOT = Path(__file__).resolve().parent.parent


def uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def one(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


# ---------------------------------------------------------------- normalize_host

@pytest.mark.parametrize("raw,expected", [
    ("ruud.com", "ruud.com"),
    ("RUUD.COM", "ruud.com"),
    ("  Ruud.Com  ", "ruud.com"),
    ("ruud.com.", "ruud.com"),
    ("Files.MyRheem.com", "files.myrheem.com"),
    ("bücher.example", "xn--bcher-kva.example"),
    ("BÜCHER.example", "xn--bcher-kva.example"),
    ("xn--bcher-kva.example", "xn--bcher-kva.example"),
])
def test_normalize_host(raw, expected):
    from crawler.hosts import normalize_host
    assert normalize_host(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "ruud.com/path", "http://ruud.com", "ruud.com:8080", "ru ud.com",
    "a..b.com", "user@ruud.com", "ruud.com?x=1",
])
def test_normalize_host_rejects_anything_that_is_not_a_bare_host(raw):
    from crawler.hosts import normalize_host
    with pytest.raises(ValueError):
        normalize_host(raw)


# ---------------------------------------------------------------- in_scope

@pytest.mark.parametrize("url", [
    "http://ruud.com/",
    "https://ruud.com/product/ra15az",
    "https://RUUD.com/x",
    "https://www.ruud.com/x",
    "https://a.b.ruud.com/x",
    "http://ruud.com:8080/x",
    "http://ruud.com./x",
])
def test_in_scope_accepts_the_approved_host_and_true_subdomains(url):
    from crawler.hosts import in_scope
    assert in_scope(url, ["ruud.com"]) is True


@pytest.mark.parametrize("url", [
    "http://evilruud.com/",
    "http://xruud.com/",
    "http://ruud.com.evil.test/",
    "http://ruud.co/",
    "http://evil.test/ruud.com",
    "http://evil.test/?next=ruud.com",
    "http://ruud.com@evil.test/",
    "http://ruud.com%2eevil.test/",
    "ftp://ruud.com/file",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "http://[::1]/",
    "http://127.0.0.1/",
    "",
])
def test_in_scope_refuses_look_alikes_other_schemes_and_junk(url):
    from crawler.hosts import in_scope
    assert in_scope(url, ["ruud.com"]) is False


def test_in_scope_with_no_approved_hosts_is_always_false():
    from crawler.hosts import in_scope
    assert in_scope("http://ruud.com/", []) is False


def test_in_scope_compares_punycode():
    from crawler.hosts import in_scope
    assert in_scope("http://BÜCHER.example/", ["xn--bcher-kva.example"]) is True
    assert in_scope("http://www.xn--bcher-kva.example/", ["bücher.example"]) is True


# ---------------------------------------------------------------- AHRI refused list

AHRI_HOSTS = ["ahridirectory.org", "www.ahridirectory.org", "AHRIDIRECTORY.ORG", "ahridirectory.org.", "ahrinet.org", "www.ahrinet.org"]


@pytest.mark.parametrize("host", AHRI_HOSTS)
def test_is_refused_host_covers_ahri(host):
    from crawler.hosts import is_refused_host
    assert is_refused_host(host) is True


def test_is_refused_host_does_not_refuse_ordinary_hosts():
    from crawler.hosts import is_refused_host
    assert is_refused_host("ruud.com") is False
    assert is_refused_host("files.myrheem.com") is False


def test_in_scope_refuses_ahri_even_if_somehow_approved():
    from crawler.hosts import in_scope
    assert in_scope("https://www.ahridirectory.org/x", ["ahridirectory.org"]) is False


# ---------------------------------------------------------------- add_brand / add_start_url

def test_add_brand_creates_one_brand_and_is_idempotent(runtime_conn):
    from crawler.brands import add_brand
    name = uniq("[T] Ruud")
    a = add_brand(runtime_conn, name)
    b = add_brand(runtime_conn, "  " + name.upper() + "  ")
    assert a == b
    rows = one(runtime_conn, "SELECT name, status FROM brands WHERE id = %s", (a,))
    assert rows == ((name, "active"),)
    assert one(runtime_conn, "SELECT COUNT(*) FROM brands WHERE name_key = %s", (name.lower(),))[0][0] == 1


def test_add_brand_does_not_approve_or_create_any_host(runtime_conn):
    from crawler.brands import add_brand
    brand_id = add_brand(runtime_conn, uniq("[T] Ruud"))
    assert one(runtime_conn, "SELECT COUNT(*) FROM brand_domains WHERE brand_id = %s", (brand_id,))[0][0] == 0


def test_add_start_url_is_stored_once_and_found_by_user(runtime_conn):
    from crawler.brands import add_brand, add_start_url
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    url = "https://www.ruud.com/products/hvac/air_conditioners/"
    first = add_start_url(runtime_conn, name, url)
    second = add_start_url(runtime_conn, name, url)
    assert first == second
    rows = one(runtime_conn, "SELECT url, found_by, status FROM start_urls WHERE brand_id = %s", (brand_id,))
    assert rows == ((url, "user", "active"),)


@pytest.mark.parametrize("bad", ["ftp://ruud.com/x", "file:///etc/passwd", "ruud.com/x", ""])
def test_add_start_url_refuses_non_http_urls(runtime_conn, bad):
    from crawler.brands import add_brand, add_start_url
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    with pytest.raises(ValueError):
        add_start_url(runtime_conn, name, bad)
    assert one(runtime_conn, "SELECT COUNT(*) FROM start_urls WHERE brand_id = %s", (brand_id,))[0][0] == 0


def test_add_start_url_for_unknown_brand_fails(runtime_conn):
    from crawler.brands import UnknownTarget, add_start_url
    with pytest.raises(UnknownTarget):
        add_start_url(runtime_conn, uniq("[T] nobody"), "https://ruud.com/")


# ---------------------------------------------------------------- approve_host (ok)

def test_ok_creates_an_approved_user_host_with_a_normalised_name(runtime_conn):
    from crawler.brands import add_brand, approve_host
    name = uniq("[T] Bücher")
    brand_id = add_brand(runtime_conn, name)
    host = approve_host(runtime_conn, "BÜCHER.example.", brand=name)
    assert host == "xn--bcher-kva.example"
    rows = one(runtime_conn, "SELECT host, status, found_by FROM brand_domains WHERE brand_id = %s", (brand_id,))
    assert rows == (("xn--bcher-kva.example", "approved", "user"),)


def test_ok_approves_a_waiting_host_in_place_and_keeps_who_found_it(runtime_conn):
    from crawler.brands import add_brand, approve_host
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    with runtime_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO brand_domains (brand_id, host, status, found_by, evidence_url) "
            "VALUES (%s, 'files.myrheem.com', 'waiting', 'ai', 'https://ruud.com/product/x')", (brand_id,))
    approve_host(runtime_conn, "Files.MyRheem.com")
    rows = one(runtime_conn,
               "SELECT host, status, found_by, evidence_url FROM brand_domains WHERE brand_id = %s", (brand_id,))
    assert rows == (("files.myrheem.com", "approved", "ai", "https://ruud.com/product/x"),)


def test_ok_twice_is_one_row(runtime_conn):
    from crawler.brands import add_brand, approve_host
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    approve_host(runtime_conn, "ruud.com", brand=name)
    approve_host(runtime_conn, "RUUD.com", brand=name)
    assert one(runtime_conn, "SELECT COUNT(*) FROM brand_domains WHERE brand_id = %s", (brand_id,))[0][0] == 1


def test_ok_for_a_host_nobody_has_seen_and_no_brand_fails(runtime_conn):
    from crawler.brands import HostUnknown, approve_host
    host = uniq("never-seen") + ".test"
    with pytest.raises(HostUnknown):
        approve_host(runtime_conn, host)
    assert one(runtime_conn, "SELECT COUNT(*) FROM brand_domains WHERE host = %s", (host,))[0][0] == 0


def test_ok_for_an_invalid_host_fails_and_writes_nothing(runtime_conn):
    from crawler.brands import add_brand, approve_host
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    with pytest.raises(ValueError):
        approve_host(runtime_conn, "http://ruud.com/x", brand=name)
    assert one(runtime_conn, "SELECT COUNT(*) FROM brand_domains WHERE brand_id = %s", (brand_id,))[0][0] == 0


@pytest.mark.parametrize("host", AHRI_HOSTS)
def test_ok_refuses_ahri_even_when_typed_and_writes_nothing(runtime_conn, host):
    from crawler.brands import add_brand, approve_host
    from crawler.hosts import RefusedHost
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    with pytest.raises(RefusedHost):
        approve_host(runtime_conn, host, brand=name)
    assert one(runtime_conn, "SELECT COUNT(*) FROM brand_domains WHERE brand_id = %s", (brand_id,))[0][0] == 0
    # Control: the same call for an ordinary host does write, so the zero above means the refusal ran.
    approve_host(runtime_conn, "ruud.com", brand=name)
    assert one(runtime_conn, "SELECT COUNT(*) FROM brand_domains WHERE brand_id = %s", (brand_id,))[0][0] == 1


def test_ok_refuses_to_approve_a_waiting_ahri_row_and_leaves_it_waiting(runtime_conn):
    from crawler.brands import add_brand, approve_host
    from crawler.hosts import RefusedHost
    brand_id = add_brand(runtime_conn, uniq("[T] Ruud"))
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) "
                    "VALUES (%s, 'ahridirectory.org', 'waiting', 'ai')", (brand_id,))
    with pytest.raises(RefusedHost):
        approve_host(runtime_conn, "ahridirectory.org")
    rows = one(runtime_conn, "SELECT status FROM brand_domains WHERE brand_id = %s", (brand_id,))
    assert rows == (("waiting",),)


# ---------------------------------------------------------------- stop

def test_stop_a_host_marks_it_stopped_and_deletes_nothing(runtime_conn):
    from crawler.brands import add_brand, approve_host, stop
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    approve_host(runtime_conn, "ruud.com", brand=name)
    approve_host(runtime_conn, "files.myrheem.com", brand=name)
    stop(runtime_conn, "RUUD.com")
    rows = dict(one(runtime_conn, "SELECT host, status FROM brand_domains WHERE brand_id = %s", (brand_id,)))
    assert rows == {"ruud.com": "stopped", "files.myrheem.com": "approved"}


def test_stop_a_brand_stops_the_brand_and_its_start_urls_and_deletes_nothing(runtime_conn):
    from crawler.brands import add_brand, add_start_url, approve_host, stop
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    add_start_url(runtime_conn, name, "https://ruud.com/a/")
    add_start_url(runtime_conn, name, "https://ruud.com/b/")
    approve_host(runtime_conn, "ruud.com", brand=name)
    stop(runtime_conn, name)
    assert one(runtime_conn, "SELECT status FROM brands WHERE id = %s", (brand_id,)) == (("stopped",),)
    assert one(runtime_conn, "SELECT COUNT(*), COUNT(*) FILTER (WHERE status = 'stopped') FROM start_urls WHERE brand_id = %s",
               (brand_id,))[0] == (2, 2)
    assert one(runtime_conn, "SELECT COUNT(*) FROM brand_domains WHERE brand_id = %s", (brand_id,))[0][0] == 1


def test_stop_something_unknown_fails(runtime_conn):
    from crawler.brands import UnknownTarget, stop
    with pytest.raises(UnknownTarget):
        stop(runtime_conn, uniq("[T] nobody"))


# ---------------------------------------------------------------- status

def test_status_reports_hosts_waiting_and_page_counts_per_brand(runtime_conn):
    from crawler.brands import add_brand, status
    name = uniq("[T] Ruud")
    brand_id = add_brand(runtime_conn, name)
    with runtime_conn.cursor() as cur:
        for host, st in (("one.test", "waiting"), ("two.test", "waiting"), ("three.test", "approved")):
            cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, %s, %s, 'ai')",
                        (brand_id, host, st))
        cur.execute("INSERT INTO hosts (host) VALUES ('three.test') ON CONFLICT (host) DO UPDATE SET host = EXCLUDED.host "
                    "RETURNING id")
        host_id = cur.fetchone()[0]
        for n, (fetched, code) in enumerate((("2026-01-01 12:00:00", 200), (None, None), ("2026-01-01 12:00:00", 404))):
            cur.execute(
                "INSERT INTO pages (brand_id, host_id, url, url_sha, fetched_at, http_status) "
                "VALUES (%s, %s, %s, encode(sha256(convert_to(%s, 'UTF8')), 'hex'), %s, %s)",
                (brand_id, host_id, f"http://three.test/{n}", f"http://three.test/{n}", fetched, code))
    mine = [row for row in status(runtime_conn) if row["brand"] == name]
    assert len(mine) == 1
    row = mine[0]
    assert row["hosts_waiting"] == 2
    assert row["pages_done"] == 1
    assert row["pages_waiting"] == 1
    assert row["pages_failed"] == 1


# ---------------------------------------------------------------- the command line

def crawl(*args):
    env = clean_env(EQ_DB_NAME=db_name())
    return subprocess.run([sys.executable, "-m", "crawler", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_cli_ok_refuses_ahri_with_a_nonzero_exit_and_a_message():
    result = crawl("ok", "ahridirectory.org", "--brand", uniq("cli brand"))
    assert result.returncode != 0
    assert "refus" in (result.stdout + result.stderr).lower()


def test_cli_add_ok_status_stop_round_trip(runtime_conn):
    brand = uniq("[T] cli brand")
    host = uniq("cli-host") + ".test"

    def one(conn, sql, args=()):
        conn.rollback()  # end the old snapshot so this read sees what the CLI just committed
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    added = crawl("add", brand, "https://" + host + "/products/")
    assert added.returncode == 0, added.stdout + added.stderr
    assert one(runtime_conn, "SELECT status FROM brands WHERE name_key = %s", (brand.lower(),)) == (("active",),)
    assert one(runtime_conn, "SELECT COUNT(*) FROM start_urls s JOIN brands b ON b.id = s.brand_id "
                             "WHERE b.name_key = %s", (brand.lower(),))[0][0] == 1

    ok = crawl("ok", host, "--brand", brand)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert one(runtime_conn, "SELECT status FROM brand_domains WHERE host = %s", (host,)) == (("approved",),)

    shown = crawl("status")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert brand in shown.stdout

    stopped = crawl("stop", host)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert one(runtime_conn, "SELECT status FROM brand_domains WHERE host = %s", (host,)) == (("stopped",),)

    missing = crawl("stop", uniq("[T] nobody"))
    assert missing.returncode != 0
