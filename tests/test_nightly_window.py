"""P24 (D91, user 2026-10-31: "lets to 2am to 6 am"): the nightly run works from 2 to 6 AM Pacific and picks up where
it left off. `./crawl auto --nightly` (the server's cron, every minute since 2026-11-01) starts once a night, at the
Settings tab's start time Pacific (hour and minute) or the first call after it, until the stop time; at 6 AM it starts no new brand, page, manual or
tagging wait, writes its report and ends. Work is saved as it goes, so what it did not reach is simply still due.
Brands take turns: the next run starts with the brand whose last turn is oldest; priority orders brands with the same.
A plain `./crawl auto` (by hand) has no window. Fake web server, fake AI, rolled back after each test."""
from datetime import datetime, timedelta

import psycopg
import pytest

from fakeai import FakeAI
from p11support import Checker, serve, serve_pdf, start, world  # noqa: F401  (world is a fixture)
from p3support import new_brand, q
from test_p11b_tagging import tags
from test_p12_build import PAGES, document
from test_p14_search import BagEmbedder

T = datetime(2031, 7, 1, 9, 30)  # 2:30 AM PDT


@pytest.fixture
def admin_conn():
    from support import connect
    conn = connect("admin", autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture(autouse=True)
def _no_window_left_behind():
    from crawler import clock
    yield
    clock.clear()


def run(world, **kwargs):
    from crawler.auto import run_auto
    kwargs.setdefault("ai_client", FakeAI(lambda r: tags()))
    kwargs.setdefault("embedder", BagEmbedder())
    kwargs.setdefault("link_checker", Checker())
    kwargs.setdefault("sleep", lambda s: None)
    return run_auto(world.conn, fetcher=world.fetcher(), public_lists=False, **kwargs)


def a_brand_with_a_manual(world):
    serve(world, "/start", "/manual.pdf")
    serve_pdf(world, "/manual.pdf")
    start(world)


# ---------------------------------------------------------------- the window

@pytest.mark.parametrize("utc,window", [
    (datetime(2026, 7, 1, 9, 0), (datetime(2026, 7, 1, 9, 0), datetime(2026, 7, 1, 13, 0))),    # 2 AM PDT, to 6 AM
    (datetime(2026, 11, 1, 10, 0), (datetime(2026, 11, 1, 10, 0), datetime(2026, 11, 1, 14, 0))),  # clocks went back
    (datetime(2026, 7, 1, 10, 0), (datetime(2026, 7, 1, 9, 0), datetime(2026, 7, 1, 13, 0))),   # 3 AM PDT: late, still in
    (datetime(2026, 7, 1, 12, 59), (datetime(2026, 7, 1, 9, 0), datetime(2026, 7, 1, 13, 0))),  # 5:59 AM PDT
    (datetime(2026, 7, 1, 13, 0), None),                                                        # 6 AM PDT: over
    (datetime(2026, 11, 1, 9, 0), None),                                                        # 1 AM PST: too early
    (datetime(2026, 1, 15, 9, 59), None),                                                       # 1:59 AM PST
])
def test_a_nightly_run_may_start_from_2_am_pacific_until_6(utc, window):
    from crawler.clock import nightly_window
    assert nightly_window(utc) == window


def test_the_window_goes_down_to_the_minute():
    """User 2026-11-01: "support down to the minute"."""
    from crawler.clock import nightly_window
    start, stop = (2, 30), (6, 45)
    assert nightly_window(datetime(2026, 7, 1, 9, 29), start, stop) is None  # 2:29 AM PDT
    assert nightly_window(datetime(2026, 7, 1, 9, 30), start, stop) == (datetime(2026, 7, 1, 9, 30),
                                                                         datetime(2026, 7, 1, 13, 45))
    assert nightly_window(datetime(2026, 7, 1, 13, 44), start, stop) is not None  # 6:44 AM PDT
    assert nightly_window(datetime(2026, 7, 1, 13, 45), start, stop) is None


def test_with_no_window_time_is_never_up():
    from crawler import clock
    clock.clear()
    assert clock.time_up() is False


def test_time_is_up_at_the_stop_time():
    from crawler import clock
    clock.set_stop(T + timedelta(hours=1), now=lambda: T)
    assert clock.time_up() is False
    clock.set_stop(T + timedelta(hours=1), now=lambda: T + timedelta(hours=1))
    assert clock.time_up() is True


# ---------------------------------------------------------------- stopping and taking turns

def a_second_brand(world):
    name, brand_id = new_brand(world.conn)
    q(world.conn, "UPDATE brands SET discovered_at = now() WHERE id = %s", (brand_id,))  # no first-night search
    return name, brand_id


def test_at_6_am_no_new_brand_is_started_and_the_report_says_who_waits(world):
    a_brand_with_a_manual(world)
    other, _ = a_second_brand(world)
    ticks = []

    def now():  # the first look is before the first brand; every look after that is past 6 AM
        ticks.append(1)
        return T if len(ticks) == 1 else T + timedelta(hours=4)

    summary = run(world, stop_at=T + timedelta(hours=3, minutes=30), clock=now)
    first = next(iter(summary["brands"]))
    assert list(summary["brands"]) == [first]
    assert summary["waiting_for_tomorrow"] == [({world.brand, other} - {first}).pop()]
    report = q(world.conn, "SELECT report FROM runs WHERE id = %s", (summary["run_id"],))[0][0]
    assert "Stopped at 6 AM" in report and summary["waiting_for_tomorrow"][0] in report
    assert summary["state"] == "done"


def test_the_next_run_starts_with_the_brand_that_waited(world):
    a_brand_with_a_manual(world)
    other, _ = a_second_brand(world)
    ticks = []

    def now():
        ticks.append(1)
        return T if len(ticks) == 1 else T + timedelta(hours=4)

    first_night = run(world, stop_at=T + timedelta(hours=3, minutes=30), clock=now)
    waited = first_night["waiting_for_tomorrow"][0]
    second_night = run(world)
    assert list(second_night["brands"])[0] == waited
    assert second_night["waiting_for_tomorrow"] == []


def test_a_brand_with_higher_priority_still_goes_first_among_brands_level_on_turns(world):
    a_brand_with_a_manual(world)
    other, other_id = a_second_brand(world)
    q(world.conn, "UPDATE control.brands SET priority = 9 WHERE name_key = lower(%s)", (other,))
    summary = run(world)
    assert list(summary["brands"])[0] == other


def test_the_crawl_fetches_no_new_page_once_time_is_up(world):
    from crawler import clock
    from p11support import crawl
    a_brand_with_a_manual(world)
    clock.set_stop(T, now=lambda: T)
    assert crawl(world)["fetched"] == 0
    clock.clear()
    assert crawl(world)["fetched"] >= 1  # still due: the next run picks it up


def test_waiting_for_tagging_answers_ends_at_6_am(world):
    a_brand_with_a_manual(world)

    class Slow(FakeAI):
        def batch_results(self, batch_id):
            return None

    clock_at = [T]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock_at[0] += timedelta(seconds=seconds)

    summary = run(world, ai_client=Slow(lambda r: tags()), sleep=sleep, wait_seconds=7200, poll_seconds=300,
                  stop_at=T + timedelta(seconds=600), clock=lambda: clock_at[0])
    assert sum(slept) == 600  # not the 2 hours it waits without a window
    assert summary["brands"][world.brand]["waiting_for_ai"] == 1  # collected by the next run, already paid for


def test_sections_start_no_new_manual_once_time_is_up(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler import clock
    from crawler.kb import build_and_publish
    doc, _, _ = document(runtime_conn)
    monkeypatch.setattr(crawler.kb, "document_pages", lambda conn, d, **kw: PAGES)
    clock.set_stop(T, now=lambda: T)
    assert build_and_publish(runtime_conn)["built"] == 0
    clock.clear()
    assert build_and_publish(runtime_conn)["built"] >= 1


# ---------------------------------------------------------------- the command

def nightly(runtime_conn, monkeypatch, utc):
    """`./crawl auto --nightly` at utc, with the run itself swapped out: returns (code, output, the stop it was given
    or None when no run started). The year is 2030 so the test database's real runs are all before the window."""
    import crawler.auto
    import crawler.clock
    from test_p11_cli import cli
    started = []
    monkeypatch.setattr(crawler.clock, "utcnow", lambda: utc)
    monkeypatch.setattr(crawler.auto, "run_auto", lambda conn, **kw: started.append(kw["stop_at"]) or {
        "state": "skipped", "run_id": None, "brands": {}, "needs_you": []})
    code, out, err = cli(runtime_conn, "auto", "--nightly")
    assert code == 0, err
    return out, started[0] if started else None


def test_the_nightly_command_outside_the_window_starts_no_run_and_says_nothing(runtime_conn, monkeypatch):
    """The cron calls it every minute (user 2026-11-01), so every call outside the window stays out of the log."""
    before = q(runtime_conn, "SELECT COUNT(*) FROM runs")[0][0]
    out, stop = nightly(runtime_conn, monkeypatch, datetime(2030, 7, 1, 13, 0))  # 6 AM PDT
    assert (out.strip(), stop) == ("", None)
    assert q(runtime_conn, "SELECT COUNT(*) FROM runs")[0][0] == before


def test_the_nightly_command_starts_late_when_tonight_has_no_run_yet(runtime_conn, monkeypatch):
    """The server was down at 2 AM: the 3:10 call still starts tonight's run, with the same 6 AM stop."""
    out, stop = nightly(runtime_conn, monkeypatch, datetime(2030, 7, 1, 10, 10))  # 3:10 AM PDT
    assert stop == datetime(2030, 7, 1, 13, 0)


def test_the_nightly_command_starts_once_a_night(runtime_conn, monkeypatch):
    """A run already started tonight (going, done or skipped): the next minute's call does nothing and says nothing."""
    q(runtime_conn, "INSERT INTO runs (state, started_at) VALUES ('done', %s)", (datetime(2030, 7, 1, 9, 0),))
    out, stop = nightly(runtime_conn, monkeypatch, datetime(2030, 7, 1, 9, 1))
    assert (out.strip(), stop) == ("", None)


def test_last_nights_run_does_not_count_for_tonight(runtime_conn, monkeypatch):
    q(runtime_conn, "INSERT INTO runs (state, started_at) VALUES ('done', %s)", (datetime(2030, 6, 30, 9, 0),))
    out, stop = nightly(runtime_conn, monkeypatch, datetime(2030, 7, 1, 9, 0))
    assert stop == datetime(2030, 7, 1, 13, 0)


# ---------------------------------------------------------------- the Settings tab's hours (P25, D93)

def test_a_window_that_crosses_midnight_stops_the_next_morning():
    from crawler.clock import nightly_window
    late = (datetime(2026, 7, 2, 5, 30), datetime(2026, 7, 2, 13, 0))  # 10:30 PM PDT -> 6 AM PDT
    assert nightly_window(datetime(2026, 7, 2, 5, 30), (22, 30), (6, 0)) == late
    assert nightly_window(datetime(2026, 7, 2, 12, 0), (22, 30), (6, 0)) == late  # 5 AM PDT, the next morning
    assert nightly_window(datetime(2026, 7, 2, 5, 29), (22, 30), (6, 0)) is None  # 10:29 PM PDT


def test_the_times_come_from_the_settings_row_with_2_and_6_by_default(runtime_conn):
    from crawler.clock import window_times
    assert window_times(runtime_conn) == ((2, 0), (6, 0))


def test_the_admin_page_can_change_the_times_and_nothing_else(admin_conn):
    from crawler.clock import window_times
    with admin_conn.cursor() as cur:
        cur.execute("UPDATE control.settings SET window_start_hour = 22, window_start_minute = 15, "
                    "window_stop_hour = 5, window_stop_minute = 45 WHERE id = 1")
    assert window_times(admin_conn) == ((22, 15), (5, 45))
    from test_p1_users import refused
    assert refused(admin_conn, "INSERT INTO control.settings (id) VALUES (2)")
    assert refused(admin_conn, "DELETE FROM control.settings WHERE false")


def test_the_same_time_for_start_and_stop_is_refused(admin_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with admin_conn.cursor() as cur:
            cur.execute("UPDATE control.settings SET window_start_hour = 3, window_start_minute = 20, "
                        "window_stop_hour = 3, window_stop_minute = 20 WHERE id = 1")


def test_the_same_hour_with_different_minutes_is_a_window(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("UPDATE control.settings SET window_start_hour = 3, window_start_minute = 0, "
                    "window_stop_hour = 3, window_stop_minute = 30 WHERE id = 1")


def test_a_minute_past_59_is_refused(admin_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with admin_conn.cursor() as cur:
            cur.execute("UPDATE control.settings SET window_stop_minute = 60 WHERE id = 1")


def test_the_report_says_the_real_stop_time():
    from crawler.clock import stop_words
    assert stop_words(datetime(2026, 7, 1, 13, 0)) == "6 AM"   # PDT
    assert stop_words(datetime(2026, 1, 15, 5, 0)) == "9 PM"   # PST, the evening before
    assert stop_words(datetime(2026, 1, 15, 8, 0)) == "12 AM"
    assert stop_words(datetime(2026, 7, 1, 13, 45)) == "6:45 AM"


# ---------------------------------------------------------------- manuals go live in the window (P24 fix, 2026-11-01)
# Prod run 6: the brands' turns used the whole window, so the sections and vector steps after them always found time
# up, and 313 tagged manuals never went live. Now a brand's manuals are cut into sections in its own turn, and the
# vectors left from the night before are made at the start of the run.

def test_a_brands_manuals_go_live_in_its_own_turn_even_when_time_runs_out_after_it(world, monkeypatch):
    import crawler.kb
    a_brand_with_a_manual(world)
    other, _ = a_second_brand(world)
    up = [False]
    real = crawler.kb.build_and_publish

    def build_then_time_is_up(conn, **kwargs):
        out = real(conn, **kwargs)
        if kwargs.get("brand_id") is not None:
            up[0] = True  # the first brand's sections are done: time runs out now
        return out

    monkeypatch.setattr(crawler.kb, "build_and_publish", build_then_time_is_up)
    summary = run(world, stop_at=T + timedelta(hours=1), clock=lambda: T + timedelta(hours=2) if up[0] else T)
    assert list(summary["brands"]) == [world.brand] and summary["waiting_for_tomorrow"] == [other]
    live = q(world.conn, "SELECT COUNT(*) FROM sections s JOIN document_urls du ON du.document_id = s.document_id "
                         "JOIN pages p ON p.id = du.page_id WHERE p.brand_id = %s AND s.is_live", (world.brand_id,))[0][0]
    assert live > 0
    assert summary["sections_built"] >= 1


def test_vectors_left_from_last_night_are_made_at_the_start_of_the_run(world, monkeypatch):
    import crawler.embed
    a_brand_with_a_manual(world)
    run(world, embedder=None)  # last night: sections built, no vectors
    missing = q(world.conn, "SELECT COUNT(*) FROM sections s WHERE s.is_live AND NOT EXISTS (SELECT 1 FROM "
                            "section_vectors_te3s_1536 v WHERE v.section_id = s.id)")[0][0]
    assert missing > 0
    up = [False]
    real = crawler.embed.embed_pass

    def embed_then_time_is_up(*args, **kwargs):
        out = real(*args, **kwargs)
        up[0] = True
        return out

    monkeypatch.setattr(crawler.embed, "embed_pass", embed_then_time_is_up)
    summary = run(world, stop_at=T + timedelta(hours=1), clock=lambda: T + timedelta(hours=2) if up[0] else T)
    assert summary["brands"] == {}  # time ran out right after the vectors, before any turn
    assert summary["embedded"] > 0
    assert q(world.conn, "SELECT COUNT(*) FROM sections s WHERE s.is_live AND NOT EXISTS (SELECT 1 FROM "
                         "section_vectors_te3s_1536 v WHERE v.section_id = s.id)")[0][0] < missing


# ---------------------------------------------------------------- Run all the time (user 2026-11-01)

def test_run_all_the_time_starts_outside_the_times_with_no_stop(runtime_conn, monkeypatch):
    """User 2026-11-01: "a switch that if I turn it on the crawler will just run and ignore the times"."""
    import crawler.clock
    monkeypatch.setattr(crawler.clock, "nonstop", lambda conn: True)
    calls = started_a_run(runtime_conn, monkeypatch, datetime(2030, 7, 1, 20, 0))  # 1 PM PDT, outside 2-6 AM
    assert [kw["stop_at"] for kw in calls] == [None]


def started_a_run(runtime_conn, monkeypatch, utc):
    """The run_auto calls `./crawl auto --nightly` made at utc (nightly() cannot tell no run from a run with no stop)."""
    import crawler.auto
    import crawler.clock
    from test_p11_cli import cli
    calls = []
    monkeypatch.setattr(crawler.clock, "utcnow", lambda: utc)
    monkeypatch.setattr(crawler.auto, "run_auto", lambda conn, **kw: calls.append(kw) or {
        "state": "skipped", "run_id": None, "brands": {}, "needs_you": []})
    code, out, err = cli(runtime_conn, "auto", "--nightly")
    assert code == 0, err
    return calls


def test_run_all_the_time_starts_at_most_one_run_an_hour(runtime_conn, monkeypatch):
    """A run that finds nothing to do ends in minutes; the next one waits until an hour after it started."""
    import crawler.clock
    monkeypatch.setattr(crawler.clock, "nonstop", lambda conn: True)
    q(runtime_conn, "INSERT INTO runs (state, started_at, finished_at) VALUES ('done', %s, %s)",
      (datetime(2030, 7, 1, 19, 30), datetime(2030, 7, 1, 19, 35)))
    assert not started_a_run(runtime_conn, monkeypatch, datetime(2030, 7, 1, 20, 0))
    assert started_a_run(runtime_conn, monkeypatch, datetime(2030, 7, 1, 20, 31))


def test_run_all_the_time_waits_while_a_run_is_going_and_logs_nothing(runtime_conn, monkeypatch):
    """A long run (started over an hour ago) still going: the every-minute call leaves no skipped row behind."""
    import crawler.clock
    from crawler.auto import LOCK_KEY
    from support import connect
    monkeypatch.setattr(crawler.clock, "nonstop", lambda conn: True)
    other = connect("runtime", autocommit=True)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        before = q(runtime_conn, "SELECT COUNT(*) FROM runs")[0][0]
        assert not started_a_run(runtime_conn, monkeypatch, datetime(2030, 7, 1, 20, 0))
        assert q(runtime_conn, "SELECT COUNT(*) FROM runs")[0][0] == before
    finally:
        other.close()  # the lock goes with the session


def test_the_switch_is_off_by_default_and_the_admin_page_can_turn_it_on(runtime_conn, admin_conn):
    from crawler.clock import nonstop
    assert nonstop(runtime_conn) is False
    with admin_conn.cursor() as cur:
        cur.execute("UPDATE control.settings SET run_nonstop = true WHERE id = 1")
    assert nonstop(admin_conn) is True


def test_a_nightly_run_passes_a_recheck_so_the_switch_and_the_times_apply_mid_run(runtime_conn, monkeypatch):
    import crawler.auto
    import crawler.clock
    from test_p11_cli import cli
    calls = []
    monkeypatch.setattr(crawler.clock, "utcnow", lambda: datetime(2030, 7, 1, 9, 0))
    monkeypatch.setattr(crawler.auto, "run_auto", lambda conn, **kw: calls.append(kw) or {
        "state": "skipped", "run_id": None, "brands": {}, "needs_you": []})
    cli(runtime_conn, "auto", "--nightly")
    assert callable(calls[0]["recheck"])


def test_the_current_stop_follows_the_switch_and_the_times(runtime_conn, monkeypatch):
    """What a going run re-reads each minute: the switch on means no stop; off means the times' stop, or now when it
    is outside them (so a run started by the switch stops within a minute of it going off)."""
    import crawler.clock
    from crawler.clock import current_stop
    monkeypatch.setattr(crawler.clock, "nonstop", lambda conn: True)
    assert current_stop(runtime_conn, datetime(2030, 7, 1, 20, 0)) is None
    monkeypatch.setattr(crawler.clock, "nonstop", lambda conn: False)
    assert current_stop(runtime_conn, datetime(2030, 7, 1, 20, 0)) == datetime(2030, 7, 1, 20, 0)  # 1 PM PDT
    assert current_stop(runtime_conn, datetime(2030, 7, 1, 10, 0)) == datetime(2030, 7, 1, 13, 0)  # 3 AM PDT -> 6


def test_time_up_rechecks_once_a_minute():
    from crawler import clock
    t = [datetime(2030, 7, 1, 20, 0)]
    answers = []
    def recheck(now):
        answers.append(now)
        return now  # the switch went off outside the times: stop now
    clock.set_stop(None, lambda: t[0], recheck=recheck)
    assert not clock.time_up()
    t[0] += timedelta(seconds=59)
    assert not clock.time_up() and answers == []
    t[0] += timedelta(seconds=1)
    assert clock.time_up() and len(answers) == 1


def test_a_recheck_that_fails_keeps_the_stop_it_had():
    from crawler import clock
    t = [datetime(2030, 7, 1, 9, 0)]
    def broken(now):
        raise RuntimeError("database went away")
    clock.set_stop(datetime(2030, 7, 1, 13, 0), lambda: t[0], recheck=broken)
    t[0] += timedelta(minutes=5)
    assert not clock.time_up()
    t[0] = datetime(2030, 7, 1, 13, 0)
    assert clock.time_up()


def test_the_next_brand_is_crawled_while_the_first_brands_tagging_answers_are_out(world, monkeypatch):
    # prod run 6, 2026-11-01: each brand sat up to 2 hours waiting for its tagging answers before the next brand
    # started, so a 5-hour window reached a few brands. Now every brand is crawled first and the run waits once, at the
    # end, for whatever is still out.
    import crawler.auto
    a_brand_with_a_manual(world)
    other, _ = a_second_brand(world)

    class Slow(FakeAI):
        def batch_results(self, batch_id):
            return None

    events = []
    crawl = crawler.auto._crawl
    monkeypatch.setattr(crawler.auto, "_crawl", lambda conn, fetcher, name, *a: events.append(name) or crawl(
        conn, fetcher, name, *a))
    summary = run(world, ai_client=Slow(lambda r: tags()), sleep=lambda s: events.append("sleep"), wait_seconds=900,
                  poll_seconds=300)
    assert events == [world.brand, other, "sleep", "sleep", "sleep"]
    assert summary["brands"][world.brand]["waiting_for_ai"] == 1 and summary["state"] == "done"


def test_answers_that_come_back_later_are_tagged_and_built_in_the_same_run(world):
    a_brand_with_a_manual(world)
    a_second_brand(world)

    class Later(FakeAI):  # nothing is ready until the run has slept once
        ready = False

        def batch_results(self, batch_id):
            return super().batch_results(batch_id) if self.ready else None

    ai = Later(lambda r: tags())

    def sleep(seconds):
        ai.ready = True

    summary = run(world, ai_client=ai, sleep=sleep, wait_seconds=900, poll_seconds=300)
    assert summary["brands"][world.brand]["tagged"] == 1 and summary["brands"][world.brand]["waiting_for_ai"] == 0
    assert q(world.conn, "SELECT COUNT(*) FROM sections s JOIN document_urls du ON du.document_id = s.document_id "
                         "JOIN pages p ON p.id = du.page_id JOIN brands b ON b.id = p.brand_id "
                         "WHERE b.name = %s AND s.is_live", (world.brand,))[0][0] > 0
