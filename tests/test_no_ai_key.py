"""No test, and nothing a test starts, can see the real AI key, so no test can spend money."""
import os
import subprocess
import sys
from pathlib import Path

from support import AI_KEYS, clean_env

TESTS = Path(__file__).resolve().parent


def set_keys(monkeypatch):
    for key in AI_KEYS:
        monkeypatch.setenv(key, "sk-ant-test-should-never-be-seen")


def test_the_session_start_hook_removes_the_ai_keys(monkeypatch):
    import conftest
    set_keys(monkeypatch)
    assert all(k in os.environ for k in AI_KEYS)  # control: they really were set
    conftest.pytest_sessionstart(None)
    assert not [k for k in AI_KEYS if k in os.environ]


def test_clean_env_drops_the_keys_and_keeps_the_rest(monkeypatch):
    set_keys(monkeypatch)
    monkeypatch.setenv("EQ_SOMETHING_ELSE", "kept")
    env = clean_env(EQ_DB_NAME="tracforce_equipment_testing")
    assert not [k for k in AI_KEYS if k in env]
    assert env["EQ_SOMETHING_ELSE"] == "kept" and env["EQ_DB_NAME"] == "tracforce_equipment_testing"
    assert "PATH" in env


def test_a_child_started_with_clean_env_cannot_see_the_keys(monkeypatch):
    set_keys(monkeypatch)
    out = subprocess.run([sys.executable, "-c", "import os; print(' '.join(sorted(os.environ)))"],
                         env=clean_env(), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "PATH" in out.stdout.split()  # the child ran and has an environment
    assert not [k for k in AI_KEYS if k in out.stdout.split()]


def test_the_cli_helper_in_the_p2_tests_starts_the_tool_without_the_keys(monkeypatch):
    import test_p2_hosts
    set_keys(monkeypatch)
    seen = []
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs.get("env"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(test_p2_hosts.subprocess, "run", spy)
    result = test_p2_hosts.crawl("status")  # read-only
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(seen) == 1 and isinstance(seen[0], dict)
    assert not [k for k in AI_KEYS if k in seen[0]]


def test_no_test_file_hands_the_raw_environment_to_a_subprocess():
    offenders = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text()
        if "subprocess" in text and ("dict(os.environ" in text or "env=os.environ" in text):
            offenders.append(path.name)
    assert offenders == []


def test_the_openai_key_is_hidden_too():
    # P13 added OPENAI_API_KEY_CRAWLER (docker compose loads it); a test building the real embedder would spend money
    assert "OPENAI_API_KEY_CRAWLER" in AI_KEYS
