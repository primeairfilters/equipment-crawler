"""P24 (D91, user 2026-10-31: "lets to 2am to 6 am"): the nightly run works from 2 to 6 AM Pacific and picks up where
it left off. `./crawl auto --nightly` (the server's cron) starts only in the 2 AM hour, Pacific time, so one of two
cron lines (09:00 and 10:00 UTC) is the right one in summer and winter; at 6 AM it starts no new brand, page, manual or
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

@pytest.mark.parametrize("utc,stop", [
    (datetime(2026, 7, 1, 9, 30), datetime(2026, 7, 1, 13, 0)),    # 2:30 AM PDT, stops 6 AM PDT
    (datetime(2026, 11, 1, 10, 0), datetime(2026, 11, 1, 14, 0)),  # the morning the clocks went back: 2 AM PST
    (datetime(2026, 1, 15, 10, 59), datetime(2026, 1, 15, 14, 0)),  # 2:59 AM PST
    (datetime(2026, 7, 1, 10, 0), None),                           # 3 AM PDT: too late to start
    (datetime(2026, 11, 1, 9, 0), None),                           # 1 AM PST: the other cron line, too early
    (datetime(2026, 1, 15, 9, 0), None),                           # 1 AM PST
])
def test_a_nightly_run_starts_only_in_the_2_am_hour_pacific_and_stops_at_6(utc, stop):
    from crawler.clock import nightly_stop
    assert nightly_stop(utc) == stop


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

def test_the_nightly_command_outside_the_start_hour_starts_no_run_and_says_nothing(runtime_conn, monkeypatch):
    """The cron calls it every hour (P25), so the other 23 calls stay out of the log."""
    import crawler.clock
    from test_p11_cli import cli
    monkeypatch.setattr(crawler.clock, "utcnow", lambda: datetime(2026, 7, 1, 10, 0))  # 3 AM PDT
    before = q(runtime_conn, "SELECT COUNT(*) FROM runs")[0][0]
    code, out, err = cli(runtime_conn, "auto", "--nightly")
    assert code == 0, err
    assert out.strip() == ""
    assert q(runtime_conn, "SELECT COUNT(*) FROM runs")[0][0] == before


# ---------------------------------------------------------------- the Settings tab's hours (P25, D93)

def test_a_window_that_crosses_midnight_stops_the_next_morning():
    from crawler.clock import nightly_stop
    assert nightly_stop(datetime(2026, 7, 2, 5, 30), 22, 6) == datetime(2026, 7, 2, 13, 0)  # 10:30 PM PDT -> 6 AM PDT
    assert nightly_stop(datetime(2026, 7, 2, 6, 30), 22, 6) is None  # 11:30 PM PDT


def test_the_hours_come_from_the_settings_row_with_2_and_6_by_default(runtime_conn):
    from crawler.clock import window_hours
    assert window_hours(runtime_conn) == (2, 6)


def test_the_admin_page_can_change_the_hours_and_nothing_else(admin_conn):
    from crawler.clock import window_hours
    with admin_conn.cursor() as cur:
        cur.execute("UPDATE control.settings SET window_start_hour = 22, window_stop_hour = 5 WHERE id = 1")
    assert window_hours(admin_conn) == (22, 5)
    from test_p1_users import refused
    assert refused(admin_conn, "INSERT INTO control.settings (id) VALUES (2)")
    assert refused(admin_conn, "DELETE FROM control.settings WHERE false")


def test_the_same_hour_for_start_and_stop_is_refused(admin_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with admin_conn.cursor() as cur:
            cur.execute("UPDATE control.settings SET window_start_hour = 3, window_stop_hour = 3 WHERE id = 1")


def test_the_report_says_the_real_stop_time():
    from crawler.clock import stop_words
    assert stop_words(datetime(2026, 7, 1, 13, 0)) == "6 AM"   # PDT
    assert stop_words(datetime(2026, 1, 15, 5, 0)) == "9 PM"   # PST, the evening before
    assert stop_words(datetime(2026, 1, 15, 8, 0)) == "12 AM"


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
