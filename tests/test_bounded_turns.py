"""Every turn has an end (user 2026-11-16): prod run 36 sat for 6.5 hours on Grundfos at 0% CPU with nothing fetched
and no query going, so the whole run, and the next one, waited for a worker that never came back. Now the AI client has
a short timeout, a brand's turn has a time limit its loops see through time_up(), and a run stops waiting for a worker
stuck past that limit and finishes without it. The workers share the test's rolled-back connection; nothing is
committed, and nothing reaches the network."""
import threading
import time
from datetime import timedelta

from p11support import world  # noqa: F401  (a fixture)
from test_crawl_workers import more_brands, run


def test_the_ai_client_gives_up_on_a_stalled_call_instead_of_waiting_half_an_hour(monkeypatch):
    import anthropic
    from crawler.ai import AnthropicClient
    made = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: made.update(kwargs))
    AnthropicClient(api_key="test")
    assert made["timeout"] <= 300 and made["max_retries"] <= 1


def test_a_turn_limit_is_up_only_for_the_worker_whose_turn_it_is():
    from crawler import clock
    seen = {}
    clock.start_turn(timedelta(seconds=-1))  # already over
    try:
        other = threading.Thread(target=lambda: seen.update(other=clock.time_up()))
        other.start()
        other.join()
        assert clock.time_up() is True
        assert seen["other"] is False  # another worker's turn is not cut short
    finally:
        clock.end_turn()
    assert clock.time_up() is False  # and once the turn is over, no limit is left behind


def test_a_long_turn_stops_at_its_limit_and_the_other_brands_still_get_their_turns(world, monkeypatch):
    from crawler import auto, clock
    names = more_brands(world, 5)
    monkeypatch.setattr(auto, "TURN_LIMIT", timedelta(seconds=1))
    crawled = []

    def crawl(conn, fetcher, name, max_pages, link_checker, commit):
        crawled.append(name)
        if name == names[0]:  # a brand that would crawl forever without a limit
            until = time.monotonic() + 30
            while not clock.time_up() and time.monotonic() < until:
                time.sleep(0.02)
            assert clock.time_up(), "the turn limit never came"
        return {}

    monkeypatch.setattr(auto, "_crawl", crawl)
    started = time.monotonic()
    summary = run(world)
    assert time.monotonic() - started < 25
    assert summary["state"] == "done"
    assert sorted(n for n in crawled if n in names) == sorted(names)  # the limit did not end the run for the others
    assert not summary.get("waiting_for_tomorrow")


def test_a_worker_stuck_past_its_limit_is_left_behind_and_the_run_finishes(world, monkeypatch):
    from crawler import auto
    names = more_brands(world, 5)
    monkeypatch.setattr(auto, "TURN_LIMIT", timedelta(seconds=1))
    monkeypatch.setattr(auto, "STUCK_GRACE", timedelta(seconds=1))
    release = threading.Event()

    def crawl(conn, fetcher, name, max_pages, link_checker, commit):
        if name == names[0]:
            release.wait(60)  # blocked the way prod's was: no time_up() check, no timeout
        return {}

    monkeypatch.setattr(auto, "_crawl", crawl)
    started = time.monotonic()
    try:
        summary = run(world)
        assert time.monotonic() - started < 30, "the run waited for the stuck worker"
        assert summary["state"] == "done"
        assert any(names[0] in line and "stuck" in line for line in summary["needs_you"]), summary["needs_you"]
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name.startswith("crawl-"):
                t.join(30)


def test_the_crawler_prints_every_threads_stack_when_asked():
    """`docker kill -s USR1 <container>` shows where a stuck run is, in ~/crawler-auto.log."""
    import faulthandler
    import signal
    from crawler.__main__ import main
    try:
        main(["--help"])
    except SystemExit:
        pass
    # faulthandler.register returns nothing; re-registering tells us it is set by unregister's answer
    assert faulthandler.unregister(signal.SIGUSR1) is True
