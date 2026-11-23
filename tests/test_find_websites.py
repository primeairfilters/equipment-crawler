"""LIVE_CAMERA_CIRCLE.md D14: website finding goes first in a run, in the user's order, inside the $50 cap.

Before 2026-11-22 a brand's website was only looked up at the start of its own turn, after earlier turns had spent the
month's money on link checks, gap-fill and tagging. 246 of 406 prod brands waited, none looked up since the cap ran out
on Nov 11. And the 90-day redo (discover.REDO_AFTER) never ran from the nightly run at all, only by hand.

The order (the user, 2026-11-22): (1) new brands Breeze missed, (2) existing brands never looked up, (3) the 90-day
redo of brands already looked up. A brand with a start page the user typed and never looked up keeps today's rule (not
asked: control.py, P19). Fake web-search client, `_testing`, one rolled-back transaction per test.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from fakeai import FakeDiscoverAI
from p3support import new_brand, q
from test_p4_discover import GOOD

T0 = datetime(2031, 5, 10, 9, 0)


def brand(conn, *, looked_up=None, by_miss=False, start_page=False, active=True):
    name, brand_id = new_brand(conn)
    if looked_up is not None:
        q(conn, "UPDATE brands SET discovered_at = %s WHERE id = %s RETURNING id", (looked_up, brand_id))
    if by_miss:
        q(conn, "UPDATE control.brands SET added_by = 'breeze miss' WHERE crawl_brand_id = %s RETURNING id", (brand_id,))
    if start_page:
        q(conn, "INSERT INTO start_urls (brand_id, url, url_sha, found_by) VALUES (%s, %s, %s, 'user') RETURNING id",
          (brand_id, f"https://{name.lower()}.test/", name))
    if not active:
        q(conn, "UPDATE brands SET status = 'stopped' WHERE id = %s RETURNING id", (brand_id,))
    return name


def looked_up(fake):
    return [c[2] for c in fake.calls if c[0] == "discover"]


def run(conn, fake, **kw):
    from crawler.control import find_websites
    return find_websites(conn, fake, now=kw.pop("now", T0), cap_usd=kw.pop("cap_usd", Decimal("50")), **kw)


def test_websites_are_looked_up_missed_brands_first_then_never_looked_up_then_the_90_day_redo(runtime_conn):
    redo = brand(runtime_conn, looked_up=T0 - timedelta(days=91))
    never = brand(runtime_conn)
    missed = brand(runtime_conn, by_miss=True)  # added last, still goes first
    recent = brand(runtime_conn, looked_up=T0 - timedelta(days=10))
    typed = brand(runtime_conn, start_page=True)
    stopped = brand(runtime_conn, active=False)
    fake = FakeDiscoverAI(GOOD)
    run(runtime_conn, fake)
    mine = [n for n in looked_up(fake) if n in {redo, never, missed, recent, typed, stopped}]
    assert mine == [missed, never, redo]  # recent, typed-start-page and stopped brands are not asked


def test_website_finding_stops_when_the_cap_is_short(runtime_conn):
    first, second = brand(runtime_conn, by_miss=True), brand(runtime_conn, by_miss=True)
    fake = FakeDiscoverAI(GOOD)
    run(runtime_conn, fake, cap_usd=Decimal("0.01"))  # less than one lookup's worst case
    assert {first, second}.isdisjoint(looked_up(fake))
    assert q(runtime_conn, "SELECT COUNT(*) FROM brands WHERE name IN (%s, %s) AND discovered_at IS NULL",
             (first, second))[0][0] == 2


def test_a_brand_over_its_own_monthly_budget_is_skipped(runtime_conn):
    over = brand(runtime_conn, by_miss=True)
    q(runtime_conn, "INSERT INTO ai_calls (purpose, brand_id, model, input_tokens, output_tokens, cost_usd, created_at) "
                    "SELECT 'extract', id, 'claude-haiku-5-5', 1, 1, 6, %s FROM brands WHERE name = %s RETURNING id",
      (T0, over))
    fake = FakeDiscoverAI(GOOD)
    run(runtime_conn, fake)
    assert over not in looked_up(fake)


def test_a_looked_up_brand_is_marked_looked_up_and_reported(runtime_conn):
    missed = brand(runtime_conn, by_miss=True)
    out = run(runtime_conn, FakeDiscoverAI(GOOD))
    assert q(runtime_conn, "SELECT discovered_at IS NOT NULL FROM brands WHERE name = %s", (missed,))[0][0]
    assert any(r[0] == missed for r in out)
