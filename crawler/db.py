"""Connections to the equipment database on PostgreSQL. The runtime role can SELECT/INSERT/UPDATE only. Callers commit.

Every connection works in UTC and finds the crawl tables first, then kb, so SQL can name tables without a schema.
"""
import os

import psycopg

SEARCH_PATH = "crawl, kb, public"


def settings(kind):
    """Connection keywords for kind 'runtime', 'migrate' or 'breeze'. Credentials come from EQ_<KIND>_USER / _PASSWORD."""
    prefix = "EQ_" + kind.upper()
    path = "kb, public" if kind == "breeze" else SEARCH_PATH
    return {
        "host": os.environ.get("EQ_DB_HOST", "pg"),
        "port": int(os.environ.get("EQ_DB_PORT", "5432")),
        "user": os.environ[prefix + "_USER"],
        "password": os.environ[prefix + "_PASSWORD"],
        "dbname": os.environ.get("EQ_DB_NAME", "tracforce_equipment"),
        "connect_timeout": 10,
        "options": f"-c search_path={path.replace(' ', '')} -c timezone=UTC",
    }


def connect(kind="runtime", autocommit=False):
    return psycopg.connect(autocommit=autocommit, **settings(kind))


def try_lock(conn, name):
    """A session lock (held across commits) so two runs of the same job never overlap. False when another run holds it."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (name,))
        return cur.fetchone()[0]


def unlock(conn, name):
    """Gives the lock back. A transaction broken by an error is rolled back first, since nothing more can run in it."""
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
        conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))
