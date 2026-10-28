"""Applies migrations/NNNN_name.sql in order as the migrate role. Only adds; re-runs are no-ops.

Each file runs in one transaction (PostgreSQL DDL is transactional), so a file is applied whole or not at all.
Statements still use IF NOT EXISTS so a file is safe to run twice. Afterwards the monthly partitions of the
append-only logs are made for the months around today, never touching a month that already holds rows.
"""
from datetime import date, datetime, timezone
from pathlib import Path

from crawler.db import connect as db_connect

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

# (schema.table, the column it is partitioned by)
PARTITIONED = (("crawl.ai_calls", "created_at"), ("crawl.crawl_runs", "started_at"), ("kb.visibility_log", "created_at"))
MONTHS_BACK = 1
MONTHS_AHEAD = 24


def connect():
    return db_connect("migrate", autocommit=False)


def _months(today):
    first = date(today.year, today.month, 1)
    index = first.year * 12 + first.month - 1 - MONTHS_BACK
    for i in range(MONTHS_BACK + MONTHS_AHEAD + 1):
        y, m = divmod(index + i, 12)
        yield date(y, m + 1, 1)


def _next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def ensure_partitions(conn, today=None):
    """A partition per month for each log, plus a default one that catches anything outside them. Returns how many were made."""
    today = today or datetime.now(timezone.utc).date()
    made = 0
    with conn.cursor() as cur:
        for table, column in PARTITIONED:
            schema, name = table.split(".")
            cur.execute(f"CREATE TABLE IF NOT EXISTS {schema}.{name}_default PARTITION OF {table} DEFAULT")
            for start in _months(today):
                part = f"{name}_{start:%Y_%m}"
                cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = %s AND c.relname = %s", (schema, part))
                if cur.fetchone():
                    continue
                end = _next_month(start)
                # a month whose rows already sit in the default partition is left there rather than moved
                cur.execute(f"SELECT 1 FROM {schema}.{name}_default WHERE {column} >= %s AND {column} < %s LIMIT 1",
                            (start, end))
                if cur.fetchone():
                    continue
                cur.execute(f"CREATE TABLE {schema}.{part} PARTITION OF {table} FOR VALUES FROM ('{start}') TO ('{end}')")
                made += 1
    return made


def migrate():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS crawl")
            cur.execute("CREATE TABLE IF NOT EXISTS crawl.schema_migrations ("
                        "name text PRIMARY KEY, applied_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'))")
            conn.commit()
            cur.execute("SELECT name FROM crawl.schema_migrations")
            done = {row[0] for row in cur.fetchall()}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in done:
                    print(f"already applied: {path.name}")
                    continue
                text = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("--"))
                for statement in text.split(";\n"):
                    if statement.strip():
                        cur.execute(statement)
                cur.execute("INSERT INTO crawl.schema_migrations (name) VALUES (%s)", (path.name,))
                conn.commit()
                print(f"applied: {path.name}")
            made = ensure_partitions(conn)
            conn.commit()
            if made:
                print(f"made {made} monthly log partitions")
    finally:
        conn.rollback()
        conn.close()
