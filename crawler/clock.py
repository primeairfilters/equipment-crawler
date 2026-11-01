"""The nightly window (P24, D91, user 2026-10-31: 2 to 6 AM; P25, D93: the hours are set on the admin page's Settings
tab, control.settings): `./crawl auto --nightly` starts only in the start hour, Pacific time, and from the stop hour
starts no new work (the next day when the stop is earlier than the start). The stop time is a module setting so the long loops (the crawl's
pages, the tagging wait, sections, embedding, re-reads) can ask `time_up()` without every call passing it down; with
no stop set (a run by hand, the tests) time is never up. Times are naive UTC, like the database's.
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
START_HOUR = 2  # the defaults, and control.settings' (the server's cron runs --nightly every hour)
STOP_HOUR = 6

_stop_at = None
_now = None


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def nightly_stop(now_utc, start_hour=START_HOUR, stop_hour=STOP_HOUR):
    """The stop (UTC) for a nightly run starting at now_utc, or None when it is not the start hour, Pacific time."""
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(PACIFIC)
    if local.hour != start_hour:
        return None
    day = local.date() + (timedelta(days=1) if stop_hour <= start_hour else timedelta(0))
    stop = datetime.combine(day, time(stop_hour), tzinfo=PACIFIC)
    return stop.astimezone(timezone.utc).replace(tzinfo=None)


def window_hours(conn):
    """(start hour, stop hour) from the admin page's Settings tab; the defaults when it has never been set."""
    with conn.cursor() as cur:
        cur.execute("SELECT window_start_hour, window_stop_hour FROM control.settings WHERE id = 1")
        row = cur.fetchone()
    return (row[0], row[1]) if row else (START_HOUR, STOP_HOUR)


def stop_words(stop_at_utc):
    """The stop as the report says it: "6 AM" (Pacific)."""
    local = stop_at_utc.replace(tzinfo=timezone.utc).astimezone(PACIFIC)
    return f"{local.hour % 12 or 12} {'AM' if local.hour < 12 else 'PM'}"


def set_stop(at, now=None):
    global _stop_at, _now
    _stop_at, _now = at, now


def clear():
    set_stop(None)


def time_up():
    return _stop_at is not None and (_now or utcnow)() >= _stop_at
