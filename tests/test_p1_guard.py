"""P1 (P10 on PostgreSQL): the test guard refuses a non-test database and passes on tracforce_equipment_testing."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from support import TESTING_DB, NotATestDatabase, assert_test_database, clean_env

ROOT = Path(__file__).resolve().parent.parent


class FakeCursor:
    def __init__(self, server):
        self.server = server

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.server.executed.append(sql)

    def fetchone(self):
        return (self.server.reported_name,)

    def close(self):
        pass


class FakeServer:
    """Stands in for a connection; the 'server' reports whatever name we give it."""

    def __init__(self, reported_name):
        self.reported_name = reported_name
        self.executed = []

    def cursor(self):
        return FakeCursor(self)


def asked_server_for_database(server):
    return any("select current_database()" in sql.lower() for sql in server.executed)


def test_guard_refuses_a_database_the_server_calls_non_test():
    server = FakeServer("tracforce_equipment")
    with pytest.raises(NotATestDatabase, match="tracforce_equipment"):
        assert_test_database(server)
    assert asked_server_for_database(server)


def test_guard_refuses_the_dev_app_database():
    server = FakeServer("hvac_app")
    with pytest.raises(NotATestDatabase):
        assert_test_database(server)
    assert asked_server_for_database(server)


def test_guard_refuses_when_no_database_is_selected():
    server = FakeServer(None)
    with pytest.raises(NotATestDatabase):
        assert_test_database(server)
    assert asked_server_for_database(server)


def test_guard_trusts_the_server_not_the_config(monkeypatch):
    # Config claims the testing database, the server says otherwise: must refuse.
    monkeypatch.setenv("EQ_DB_NAME", TESTING_DB)
    lying_config = FakeServer("tracforce_equipment")
    with pytest.raises(NotATestDatabase):
        assert_test_database(lying_config)
    assert asked_server_for_database(lying_config)

    # Config claims the real database, the server says testing: must pass.
    monkeypatch.setenv("EQ_DB_NAME", "tracforce_equipment")
    honest_server = FakeServer(TESTING_DB)
    assert assert_test_database(honest_server) == TESTING_DB
    assert asked_server_for_database(honest_server)


def test_guard_passes_for_runtime_user_on_the_real_testing_database(runtime_conn):
    assert assert_test_database(runtime_conn) == TESTING_DB


def test_guard_passes_for_migrate_user_on_the_real_testing_database(migrate_conn):
    assert assert_test_database(migrate_conn) == TESTING_DB


def test_pytest_aborts_before_any_test_when_pointed_at_a_non_test_database():
    env = clean_env(EQ_DB_NAME="tracforce_equipment")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_p1_guard.py", "-k", "__matches_nothing__"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert "REFUSING TO RUN TESTS" in output, output
    assert "[tracforce_equipment]" in output, output
    assert result.returncode != 0
    # The run was aborted at start, not "passed with nothing selected".
    assert "no tests ran" not in output and "passed" not in output, output
