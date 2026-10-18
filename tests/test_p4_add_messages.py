"""P4: what `./crawl add` says when discovery is skipped, and that it never reaches for the real AI client in tests.

crawler.__main__._discover(conn, brand) is called directly with discover() and AnthropicClient replaced,
so no AI client is built from a key and nothing is committed (the connection is wrapped so commit does nothing).
"""
import pytest

from p3support import new_brand


class NoCommit:
    """The test's connection, with commit turned into a no-op so the test's rollback still undoes everything."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def run_add_step(monkeypatch, conn, brand, discover_result=None, no_key=False):
    import crawler.ai
    import crawler.discover
    from crawler.__main__ import _discover

    if no_key:
        def boom():
            raise RuntimeError("ANTHROPIC_API_KEY_CRAWLER is not set.")
        monkeypatch.setattr(crawler.ai, "AnthropicClient", boom)
    else:
        monkeypatch.setattr(crawler.ai, "AnthropicClient", lambda: object())
        monkeypatch.setattr(crawler.discover, "discover", lambda c, client, b, **kw: discover_result)
    _discover(NoCommit(conn), brand)


@pytest.mark.parametrize("result,words", [
    ("recent", "Already looked for this brand's websites in the last 6 months."),
    ("budget", "Monthly AI budget reached, so I skipped finding websites until the 1st."),
    ("unusable", "the AI answer could not be used"),
])
def test_add_says_in_plain_words_why_discovery_was_skipped(monkeypatch, capsys, runtime_conn, result, words):
    name, brand_id = new_brand(runtime_conn)
    run_add_step(monkeypatch, runtime_conn, name, result)
    assert words in capsys.readouterr().out


def test_add_without_an_ai_key_says_so_and_still_finishes(monkeypatch, capsys, runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    run_add_step(monkeypatch, runtime_conn, name, no_key=True)
    assert "No AI key is set" in capsys.readouterr().out


def test_add_lists_the_websites_that_are_waiting_for_ok(monkeypatch, capsys, runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, 'files.myrheem.com', 'waiting', 'ai')",
                    (brand_id,))
        cur.execute("INSERT INTO brand_domains (brand_id, host, status, found_by) VALUES (%s, 'ruud.com', 'approved', 'user')",
                    (brand_id,))
    run_add_step(monkeypatch, runtime_conn, name, {"domains": []})
    out = capsys.readouterr().out
    assert f"./crawl ok files.myrheem.com --brand {name}" in out
    assert "ruud.com" not in out  # only the ones still waiting
    assert "Nothing is fetched until you say OK" in out
