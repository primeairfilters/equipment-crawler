"""The nightly window (P24, D91, user 2026-10-31: 2 to 6 AM; P25, D93: the times are set on the admin page's Settings
tab, control.settings, to the minute since 2026-11-01): `./crawl auto --nightly`, called every minute, starts once a
night, at the start time Pacific or the first call after it, and from the stop time starts no new work (the next day
when the stop is earlier than the start). The Settings tab's "Run all the time" switch (2026-11-01) ignores the times:
a run starts whenever none is going and none started in the last hour. A nightly run re-reads the switch and the
times once a minute (`recheck`), so turning the switch off, or changing the times, applies to the run that is going. The stop time is a module setting so the long loops (the crawl's
pages, the tagging wait, sections, embedding, re-reads) can ask `time_up()` without every call passing it down; with
no stop set (a run by hand, the tests) time is never up. Times are naive UTC, like the database's.
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
START_HOUR = 2  # the defaults, and control.settings' (the server's cron runs --nightly every minute)
STOP_HOUR = 6

_stop_at = None
_now = None
_recheck = None
_checked_at = None
RECHECK_EVERY = timedelta(minutes=1)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def nightly_window(now_utc, start=(START_HOUR, 0), stop=(STOP_HOUR, 0)):
    """(start, stop) in UTC of the window now_utc is in, or None outside it. start and stop are (hour, minute),
    Pacific time."""
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(PACIFIC)
    day = local.date()
    if (local.hour, local.minute) < start:
        day -= timedelta(days=1)  # the window that began yesterday, if it crosses midnight
    begin = datetime.combine(day, time(*start), tzinfo=PACIFIC)
    end = datetime.combine(day + (timedelta(days=1) if stop <= start else timedelta(0)), time(*stop), tzinfo=PACIFIC)
    begin, end = (t.astimezone(timezone.utc).replace(tzinfo=None) for t in (begin, end))
    return (begin, end) if begin <= now_utc < end else None


def window_times(conn):
    """((start hour, minute), (stop hour, minute)) from the admin page's Settings tab; the defaults when never set."""
    with conn.cursor() as cur:
        cur.execute("SELECT window_start_hour, window_start_minute, window_stop_hour, window_stop_minute "
                    "FROM control.settings WHERE id = 1")
        row = cur.fetchone()
    return ((row[0], row[1]), (row[2], row[3])) if row else ((START_HOUR, 0), (STOP_HOUR, 0))


def nonstop(conn):
    """The Settings tab's "Run all the time" switch."""
    with conn.cursor() as cur:
        cur.execute("SELECT run_nonstop FROM control.settings WHERE id = 1")
        row = cur.fetchone()
    return bool(row and row[0])


def current_stop(conn, now_utc):
    """The stop a going nightly run should have now: None with the switch on, else the times' stop, or now when it is
    outside the times."""
    if nonstop(conn):
        return None
    window = nightly_window(now_utc, *window_times(conn))
    return window[1] if window else now_utc


def ran_since(conn, at_utc):
    """True when a run (going, done or skipped) started at or after at_utc: tonight already had its turn."""
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM runs WHERE started_at >= %s)", (at_utc,))
        return cur.fetchone()[0]


def stop_words(stop_at_utc):
    """The stop as the report says it: "6 AM", or "6:45 AM" (Pacific)."""
    local = stop_at_utc.replace(tzinfo=timezone.utc).astimezone(PACIFIC)
    minutes = f":{local.minute:02d}" if local.minute else ""
    return f"{local.hour % 12 or 12}{minutes} {'AM' if local.hour < 12 else 'PM'}"


def set_stop(at, now=None, recheck=None):
    """recheck(now) -> the new stop (or None), asked at most once a minute by time_up(); a failed one keeps the stop."""
    global _stop_at, _now, _recheck, _checked_at
    _stop_at, _now, _recheck = at, now, recheck
    _checked_at = (now or utcnow)() if recheck else None


def clear():
    set_stop(None)


def time_up():
    global _stop_at, _checked_at
    now = (_now or utcnow)()
    if _recheck is not None and now - _checked_at >= RECHECK_EVERY:
        _checked_at = now
        try:
            _stop_at = _recheck(now)
        except Exception:
            pass  # the database hiccuped: keep the stop we had and ask again in a minute
    return _stop_at is not None and now >= _stop_at
