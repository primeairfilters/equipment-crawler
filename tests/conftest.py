import os

import pytest

from support import AI_KEYS, NotATestDatabase, assert_test_database, connect, db_name


def pytest_sessionstart(session):
    """Hide the real AI key from every test, point every connection (the crawler's own connect too) at the testing
    database, then abort unless the database is a test database, per the server."""
    for key in AI_KEYS:
        os.environ.pop(key, None)
    os.environ["EQ_DB_NAME"] = db_name()
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


@pytest.fixture(scope="session", autouse=True)
def _testing_db_is_migrated():
    """`migrate` only adds. Run it once before any test opens a transaction, so columns added by later migrations
    exist on the testing database whatever test file is run alone."""
    from test_p1_migrate import migrate_ok
    migrate_ok()


@pytest.fixture(autouse=True)
def _library_in_tmp(tmp_path, monkeypatch):
    """No test writes into the real document library; tests that check it point DOCS_DIR at their own folder."""
    folder = tmp_path / "library"
    folder.mkdir()
    monkeypatch.setenv("DOCS_DIR", str(folder))
