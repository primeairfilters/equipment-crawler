"""`./crawl add-brands <file>` (P23, user 2026-10-31): load a list of brands into the admin page's table at once.

One brand per line, "name | trade, trade" (Tracforce's trade keys, from its one list: crawler/trades.py); blank lines and lines
starting with # are skipped. Names are tidied the way the admin page tidies them (trimmed, single spaces) and the same
brand twice ignoring case is one brand with both lines' trades. Every new brand is added paused, so nothing is crawled
or spent until it is resumed on the admin page. A brand already there keeps its state; it gets the list's trades only
if it has none (Ruud, added before trades), never over trades someone set. A bad line stops the whole load and names
the line. Adds only; the caller commits.
"""
import re

ADDED_BY = "brand list"


class BadList(ValueError):
    pass


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def parse(lines, known):
    """{name_key: (name, [trades])} in file order, trades in the order of `known` (Tracforce's list). Raises BadList on
    the first bad line."""
    brands = {}
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise BadList(f"line {number}: no '|' between the brand and its trades: {raw!r}")
        name, _, trades = line.partition("|")
        name = re.sub(r"\s+", " ", name).strip()
        picked = [t.strip() for t in trades.split(",") if t.strip()]
        if not name:
            raise BadList(f"line {number}: no brand name: {raw!r}")
        if not picked:
            raise BadList(f"line {number}: {name} has no trade")
        unknown = [t for t in picked if t not in known]
        if unknown:
            raise BadList(f"line {number}: {name}: not a Tracforce trade: {', '.join(unknown)} "
                          f"(one of {', '.join(known)})")
        key = name.lower()
        _, before = brands.get(key, (name, []))
        brands[key] = (brands.get(key, (name,))[0], [t for t in known if t in set(before) | set(picked)])
    return brands


def load(conn, lines, known):
    """Returns {"added", "trades_set", "already_there"}. known: Tracforce's trades list (crawler/trades.py)."""
    out = {"added": 0, "trades_set": 0, "already_there": 0}
    for key, (name, trades) in parse(lines, known).items():
        found = _rows(conn, "SELECT id, trades FROM control.brands WHERE name_key = %s", (key,))
        if not found:
            _rows(conn, "INSERT INTO control.brands (name, state, trades, added_by) VALUES (%s, 'paused', %s, %s)",
                  (name, trades, ADDED_BY))
            out["added"] += 1
        elif not found[0][1]:
            _rows(conn, "UPDATE control.brands SET trades = %s WHERE id = %s", (trades, found[0][0]))
            out["trades_set"] += 1
        else:
            out["already_there"] += 1
    return out
