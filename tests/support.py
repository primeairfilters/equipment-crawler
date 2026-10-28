"""Shared test helpers: connection settings and the test-database guard.

The guard lives here, not in the tool, so the test run's safety does not depend on code that
may be broken. It reads the database name back from the SERVER (SELECT current_database()), never
from config, because config is exactly what could be wrong.
"""
import os

import psycopg

TESTING_DB = "tracforce_equipment_testing"
SEARCH_PATH = "crawl,kb,public"

# The real AI key must never be visible to a test or to anything a test starts, so no test can spend money.
AI_KEYS = ("ANTHROPIC_API_KEY_CRAWLER", "ANTHROPIC_API_KEY")


def clean_env(**extra):
    """The environment for a subprocess a test starts: ours, without the AI keys, plus extra."""
    env = {k: v for k, v in os.environ.items() if k not in AI_KEYS}
    env.update(extra)
    return env


class NotATestDatabase(RuntimeError):
    pass


class TupleCursor(psycopg.Cursor):
    """fetchall() gives a tuple of rows, as the tests were written against (PyMySQL did the same)."""

    def fetchall(self):
        """A write gives () rather than an error, so test helpers can run any statement through one function."""
        return tuple(super().fetchall()) if self.description else ()


def db_name():
    """The database the tests ask to connect to. Defaults to the testing one."""
    return os.environ.get("EQ_DB_NAME", TESTING_DB)


def connect(kind, autocommit=False, dbname=None):
    """kind is 'runtime', 'migrate' or 'breeze'. Credentials come from EQ_<KIND>_USER / _PASSWORD."""
    prefix = "EQ_" + kind.upper()
    path = "kb,public" if kind == "breeze" else SEARCH_PATH
    return psycopg.connect(
        host=os.environ.get("EQ_DB_HOST", "pg"),
        port=int(os.environ.get("EQ_DB_PORT", "5432")),
        user=os.environ[prefix + "_USER"],
        password=os.environ[prefix + "_PASSWORD"],
        dbname=dbname or db_name(),
        autocommit=autocommit,
        connect_timeout=10,
        options=f"-c search_path={path} -c timezone=UTC",
        cursor_factory=TupleCursor,
    )


def assert_test_database(conn):
    """Abort unless the server says this connection is on a database whose name contains 'test'."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
    name = row[0] if row else None
    if not name or "test" not in name:
        raise NotATestDatabase(
            f"REFUSING TO RUN TESTS: the server says this connection is on database [{name}], "
            "which does not look like a test database."
        )
    return name
