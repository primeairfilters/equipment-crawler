import os

import pytest

from support import AI_KEYS, NotATestDatabase, assert_test_database, connect


def pytest_sessionstart(session):
    """Hide the real AI key from every test, then abort unless the database is a test database, per the server."""
    for key in AI_KEYS:
        os.environ.pop(key, None)
    try:
        conn = connect("runtime")
    except Exception as exc:
        pytest.exit(f"CANNOT CONNECT to the equipment database: {exc!r}", returncode=4)
    try:
        assert_test_database(conn)
    except NotATestDatabase as exc:
        pytest.exit(str(exc), returncode=3)
    finally:
        conn.close()


@pytest.fixture
def runtime_conn():
    """Runtime user, one open transaction, always rolled back."""
    conn = connect("runtime", autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def migrate_conn():
    """Migrate user, for read-only inspection. Autocommit so every query sees fresh data."""
    conn = connect("migrate", autocommit=True)
    try:
        yield conn
    finally:
        conn.close()
