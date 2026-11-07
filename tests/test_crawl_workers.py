"""Five brands crawled at once (user 2026-11-06: prod run 16 spent 1,504 of its 1,536 minutes crawling one site at a
time, most of it waiting out GAP_SECONDS). Each site still gets one request at a time, GAP_SECONDS apart, though two
brands share it; one PDF is read at a time (the server has 4 GB, a read may use 1 GB); tagging answers are taken in by
one worker at a time, so none is taken in twice.

The workers share the test's one rolled-back connection (psycopg runs one statement at a time on it), so nothing is
committed. No network: the crawl and the reader's child process are fakes that only count how many run at once."""
import threading
import time
import uuid

from p11support import world  # noqa: F401  (a fixture)


class AtOnce:
    """Counts how many callers are inside at once. Each caller waits up to `hold` seconds for `want` to be inside."""

    def __init__(self, want=1, hold=0.2):
        self.want, self.hold = want, hold
        self.inside = self.most = 0
        self.lock = threading.Lock()

    def __enter__(self):
        with self.lock:
            self.inside += 1
            self.most = max(self.most, self.inside)
        until = time.monotonic() + self.hold
        while time.monotonic() < until and self.inside < self.want:
            time.sleep(0.01)
        if self.want == 1:
            time.sleep(self.hold)
        return self

    def __exit__(self, *exc):
        with self.lock:
            self.inside -= 1


def more_brands(world, n):
    """The world's brand and n more: their names."""
    from crawler.brands import add_brand
    names = [world.brand] + [f"[T] Brand {uuid.uuid4().hex[:8]}" for _ in range(n)]
    for name in names[1:]:
        add_brand(world.conn, name)
    return names


def run(world, **kwargs):
    from crawler.auto import run_auto
    return run_auto(world.conn, fetcher=world.fetcher(), sleep=lambda s: None, public_lists=False, workers=5,
                    connect=lambda: world.conn, make_fetcher=lambda conn: world.fetcher(), **kwargs)


def test_five_brands_are_crawled_at_once_each_once_and_their_pdfs_are_read_one_at_a_time(world, monkeypatch):
    from crawler import auto, textextract
    names = more_brands(world, 5)
    crawls, reads, crawled = AtOnce(want=5, hold=2), AtOnce(hold=0.2), []

    class Child:  # the PDF reader's child process
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.pid = 0

        def communicate(self, data=None, timeout=None):
            with reads:
                return b'{"pages": []}', b""

    monkeypatch.setattr(textextract.subprocess, "Popen", Child)

    def crawl(conn, fetcher, name, max_pages, link_checker, commit):
        with crawls:
            crawled.append(name)
            textextract._run_child(b"%PDF", ["1", "layout"], 30, 1)
        return {}

    monkeypatch.setattr(auto, "_crawl", crawl)
    summary = run(world)
    assert summary["state"] == "done" and summary["needs_you"] == []
    assert sorted(n for n in crawled if n in names) == sorted(names)  # every brand, once
    assert crawls.most == 5  # five at once, not one after another
    assert reads.most == 1  # but one PDF read at a time


def test_tagging_is_sent_and_answers_are_taken_in_by_one_worker_at_a_time(world, monkeypatch):
    from crawler import auto
    from fakeai import FakeAI
    more_brands(world, 5)
    ai = AtOnce(hold=0.1)

    def tag(conn, client, name, commit):
        with ai:
            return 0

    def collect(conn, client, commit):
        with ai:
            return {}

    monkeypatch.setattr(auto, "_crawl", lambda *a: {})
    monkeypatch.setattr(auto, "_discover", lambda *a: None)
    monkeypatch.setattr(auto, "_tag", tag)
    monkeypatch.setattr(auto, "_collect_answers", collect)
    summary = run(world, ai_client=FakeAI(lambda r: {}))
    assert summary["state"] == "done"
    assert ai.most == 1


def test_a_site_another_worker_is_fetching_from_waits_until_that_request_is_done_and_the_gap_after_it(world):
    """Two brands share a site (Carrier and Bryant): the second worker's request waits for the first one's to end, and
    GAP_SECONDS more, though the first has not committed its wait to the database yet."""
    from datetime import timedelta

    from crawler.fetch import GAP_SECONDS
    first, second = world.fetcher(), world.fetcher()
    host_id = first._host_row("ruud.test")["id"]
    first._wait_gap(host_id)  # the first worker's request starts, and is still going
    done_at = []

    def second_sleeps(seconds):
        world.clock.sleep(seconds)
        if not done_at:  # meanwhile the first request ends, as _get's finally records it
            done_at.append(world.clock.now())
            first._exec("UPDATE hosts SET last_request_at = %s, next_allowed_at = %s WHERE id = %s",
                        (done_at[0], done_at[0] + timedelta(seconds=GAP_SECONDS), host_id))

    second.sleep = second_sleeps
    started = world.clock.now()
    second._wait_gap(host_id)
    assert done_at, "the second request went while the first was still going"
    assert (world.clock.now() - done_at[0]).total_seconds() >= GAP_SECONDS > 0
    assert world.clock.now() > started


def test_each_worker_shows_the_brand_it_is_crawling_and_no_brand_once_it_is_done(world, monkeypatch):
    """The admin page's five cards (user 2026-11-06) read control.crawl_workers."""
    from crawler import auto
    from p3support import q
    names = more_brands(world, 5)
    seen = []

    def crawl(conn, fetcher, name, max_pages, link_checker, commit):
        seen.append((name, [b for (b,) in q(conn, "SELECT brand FROM control.crawl_workers WHERE brand IS NOT NULL")]))
        return {}

    monkeypatch.setattr(auto, "_crawl", crawl)
    run(world)
    assert all(name in showing for name, showing in seen if name in names)  # each brand on a card while it crawls
    assert q(world.conn, "SELECT COUNT(*), COUNT(brand) FROM control.crawl_workers") == ((5, 0),)  # all idle after


def test_the_cards_show_a_brand_only_while_a_run_is_going(runtime_conn):
    """A run that crashed leaves its workers' brands behind; the admin page's view shows them idle once no run is
    going, so no card is stuck on a brand."""
    from p3support import q
    q(runtime_conn, "INSERT INTO control.crawl_workers (worker, brand, since) VALUES (1, 'Carrier', now()), (2, NULL, NULL) "
                    "ON CONFLICT (worker) DO UPDATE SET brand = EXCLUDED.brand, since = EXCLUDED.since")
    q(runtime_conn, "UPDATE runs SET state = 'interrupted' WHERE state = 'running'")
    cards = "SELECT worker, brand, since IS NOT NULL FROM control.worker_cards WHERE worker IN (1, 2) ORDER BY worker"
    assert q(runtime_conn, cards) == ((1, None, False), (2, None, False))
    q(runtime_conn, "INSERT INTO runs (state) VALUES ('running')")
    assert q(runtime_conn, cards) == ((1, "Carrier", True), (2, None, False))


def test_the_workers_start_crawling_while_the_start_of_the_run_is_still_going(world, monkeypatch):
    """Prod run 19 (2026-11-07): the cards sat idle while the run re-read every PDF with a parse issue before any brand
    was crawled (user: "I want them to be able to just keep running"). Uploads, the parse-issue retry and last night's
    vectors now go on beside the workers."""
    from crawler import auto
    more_brands(world, 5)
    crawling = threading.Event()
    seen = {}

    def crawl(conn, fetcher, name, max_pages, link_checker, commit):
        crawling.set()
        return {}

    def retry(conn, commit):
        seen["crawling during the retry"] = crawling.wait(timeout=3)
        return {}

    monkeypatch.setattr(auto, "_crawl", crawl)
    monkeypatch.setattr(auto, "_retry_issues", retry)
    summary = run(world)
    assert summary["state"] == "done" and summary["needs_you"] == []
    assert seen == {"crawling during the retry": True}
