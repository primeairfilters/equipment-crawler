"""P11: the commands for depth, feeds, keywords, verdicts, the robots and browser switches, and the new status lines.

Each command runs through the real main() on the test's own connection, wrapped so commit and close do nothing.
"""
import contextlib
import io
from unittest import mock

from p3support import new_brand, q


class NoCommit:
    def __init__(self, inner):
        self._inner = inner

    def cursor(self, *a, **k):
        return self._inner.cursor(*a, **k)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


def cli(conn, *args):
    from crawler.__main__ import main
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("crawler.db.connect", lambda *a, **k: NoCommit(conn)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


def test_add_takes_a_depth_and_a_feed_flag(runtime_conn, monkeypatch):
    import crawler.__main__
    monkeypatch.setattr(crawler.__main__, "_discover", lambda conn, brand: None)
    name, brand_id = new_brand(runtime_conn)
    assert cli(runtime_conn, "add", name, "https://www.ruud.com/products/", "--depth", "5")[0] == 0
    assert cli(runtime_conn, "add", name, "https://www.ruud.com/api/v0/menu", "--feed")[0] == 0
    assert q(runtime_conn, "SELECT url, depth, kind FROM start_urls WHERE brand_id = %s ORDER BY id", (brand_id,)) == (
        ("https://www.ruud.com/products/", 5, "page"), ("https://www.ruud.com/api/v0/menu", 3, "feed"))
    code, out, err = cli(runtime_conn, "add", name, "https://www.ruud.com/x", "--depth", "11")
    assert code != 0


def test_ok_flips_the_robots_and_browser_switches_and_says_so(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    code, out, _ = cli(runtime_conn, "ok", "www.york.com", "--brand", name, "--ignore-robots", "--browser")
    assert code == 0 and "ignoring robots.txt" in out.lower() and "browser" in out.lower()
    assert q(runtime_conn, "SELECT ignore_robots, use_browser FROM hosts WHERE host = 'www.york.com'") == ((True, True),)
    code, out, _ = cli(runtime_conn, "ok", "www.york.com", "--obey-robots", "--no-browser")
    assert code == 0
    assert q(runtime_conn, "SELECT ignore_robots, use_browser FROM hosts WHERE host = 'www.york.com'") == ((False, False),)


def test_skip_add_off_and_list(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    word = "zz" + name.lower()[-6:]
    assert cli(runtime_conn, "skip", "add", word + "*", "--note", "test words")[0] == 0
    assert cli(runtime_conn, "skip", "add", word + "b", "--brand", name)[0] == 0
    code, out, _ = cli(runtime_conn, "skip", "list")
    assert code == 0 and word + "*" in out and "test words" in out and word + "b" in out and name in out
    assert cli(runtime_conn, "skip", "off", word + "*")[0] == 0
    assert q(runtime_conn, "SELECT active FROM crawl_keywords WHERE keyword = %s", (word + "*",)) == ((False,),)  # kept
    assert cli(runtime_conn, "skip", "add", word + "*")[0] == 0  # adding it again switches it back on
    assert q(runtime_conn, "SELECT active FROM crawl_keywords WHERE keyword = %s", (word + "*",)) == ((True,),)


def test_a_keyword_that_is_not_one_word_is_refused(runtime_conn):
    for bad in ("two words", "a/b", "*", "", "blog**"):
        code, out, err = cli(runtime_conn, "skip", "add", bad)
        assert code != 0, bad


def test_prefer_keywords_work_the_same_way(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    word = "pp" + name.lower()[-6:]
    assert cli(runtime_conn, "prefer", "add", word)[0] == 0
    assert q(runtime_conn, "SELECT effect, source, active FROM crawl_keywords WHERE keyword = %s", (word,)) == (
        ("prefer", "user", True),)


def test_verdict_on_an_address_never_seen_says_so(runtime_conn):
    code, out, err = cli(runtime_conn, "verdict", "https://never.seen.test/x", "crawl")
    assert code != 0 and "never" in err.lower()


def test_status_shows_what_was_missed_and_why(runtime_conn):
    from crawler.brands import status
    name, brand_id = new_brand(runtime_conn)
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO hosts (host) VALUES ('s.test') ON CONFLICT (host) DO UPDATE SET host = 'server.test' RETURNING id")
        host_id = cur.fetchone()[0]
        cur.execute("INSERT INTO start_urls (brand_id, url, url_sha, found_by) VALUES (%s, 'http://s.test/', 'x', 'user') "
                    "RETURNING id", (brand_id,))
        start_id = cur.fetchone()[0]
        cur.execute("INSERT INTO crawl_runs (started_at, brand_id, start_url_id, limit_hit, links_left) "
                    "VALUES (now(), %s, %s, true, 40)", (brand_id, start_id))
        for n, (url, fetch_state, reason) in enumerate([
                ("http://s.test/a.pdf", "beyond_depth", None), ("http://s.test/b.pdf", "beyond_depth", None),
                ("http://s.test/c", "beyond_depth", None), ("http://s.test/blog/1", "skipped_keyword", "skip keyword 'blog'"),
                ("http://s.test/blog/2", "skipped_keyword", "skip keyword 'blog'"), ("http://s.test/q", "ask_ai", None)]):
            cur.execute("INSERT INTO pages (brand_id, host_id, url, url_sha, fetch_state, decision_reason) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", (brand_id, host_id, url, f"sha{n}", fetch_state, reason))
        cur.execute("INSERT INTO keyword_suggestions (brand_id, keyword, times) VALUES (%s, 'careers', 3)", (brand_id,))
    row = [r for r in status(runtime_conn) if r["brand"] == name][0]
    assert row["starts_at_limit"] == 1 and row["pdf_links_beyond_depth"] == 2 and row["waiting_for_ai"] == 1
    assert row["keyword_skips"] == {"skip keyword 'blog'": 2}
    assert row["suggested_keywords"] == ["careers"]
    code, out, _ = cli(runtime_conn, "status")
    line = [ln for ln in out.splitlines() if name in ln][0]
    detail = out.split(line, 1)[1]
    assert "1 start address hit the page limit" in detail
    assert "2 PDF links beyond depth" in detail
    assert "1 link waiting for the AI" in detail
    assert "skip keyword 'blog': 2" in detail
    assert "suggested skip keywords: careers" in detail.lower()


def test_run_takes_a_page_limit_for_small_test_runs(runtime_conn, monkeypatch):
    import crawler.crawl
    import crawler.sitemaps
    seen = {}
    monkeypatch.setattr(crawler.crawl, "run", lambda conn, fetcher, **kw: seen.update(kw) or {})
    monkeypatch.setattr(crawler.sitemaps, "read_sitemaps", lambda *a, **k: 0)
    name, _ = new_brand(runtime_conn)
    code, _, err = cli(runtime_conn, "run", "--brand", name, "--max-pages", "30")
    assert code == 0, err
    assert seen["max_pages"] == 30
    import pytest
    with pytest.raises(SystemExit):  # a limit of no pages is refused before anything runs
        cli(runtime_conn, "run", "--brand", name, "--max-pages", "0")
