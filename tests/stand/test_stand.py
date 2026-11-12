"""The main promises of the project, checked on a real running stack."""

import re
import time

import pytest

from .conftest import PROMPT, Term, http_json, open_term

pytestmark = pytest.mark.stand


def single_value(out: str) -> str:
    """The value from a ``select`` output with one row and one column.

    :param out: psql output.
    :return: the value on the line under the header.
    """
    match = re.search(r"^-+\n\s*(.*?)\s*\n\(1 row\)", out, re.M)
    assert match, out
    return match.group(1)


def wait_status(sid: str, status: str, timeout: float = 10) -> dict:
    """Wait for a session status in ``/api/sessions/{sid}``.

    :param sid: session id.
    :param status: the status to wait for.
    :param timeout: how long to wait, seconds.
    :return: session details.
    """
    deadline = time.monotonic() + timeout
    while True:
        info = http_json(f"/api/sessions/{sid}")
        if info["status"] == status or time.monotonic() > deadline:
            assert info["status"] == status, info
            return info
        time.sleep(0.2)


def test_query_runs_in_own_copy(term: Term):
    assert "Your own copy of the bookstore database is ready" in term.screen
    assert single_value(term.run("select count(*) from books;")) == "30"
    info = wait_status(term.sid, "running")
    assert info["database"] == f"sbx_{term.sid}"
    assert info["sandbox_ms"] > 0


def test_broken_copy_does_not_leak_into_next_session(term: Term):
    out = term.run("drop table books cascade;")
    assert "DROP TABLE" in out
    assert "does not exist" in term.run("select count(*) from books;")
    term.close()

    fresh = open_term()
    try:
        assert single_value(fresh.run("select count(*) from books;")) == "30"
    finally:
        fresh.close()


def test_copy_is_removed_after_close(term: Term):
    assert term.close() is not None
    info = wait_status(term.sid, "exited")
    assert info["finished_at"]


@pytest.mark.parametrize(("command", "expected"), [
    ("\\c postgres", "permission denied"),
    ("drop database bookstore_template;", "cannot drop a template database"),
    ("copy (select 1) to program 'id';", "permission denied"),
    ("alter system set work_mem = '1GB';", "permission denied"),
    ("create database other;", "permission denied"),
])
def test_sandbox_forbids(term: Term, command: str, expected: str):
    assert expected in term.run(command)


def test_shell_runs_as_unprivileged_user_without_secrets(term: Term):
    assert "uid=2000(sandbox)" in term.run("\\! id")
    env = term.run("\\! env")
    for secret in ("ADMIN_DSN", "REDIS_URL", "POSTGRES_PASSWORD", "web-psql@"):
        assert secret not in env
    assert "Permission denied" in term.run("\\! cat /proc/1/environ")


def test_reconnect_returns_only_missed_output(term: Term):
    term.run("select 'before the drop';")
    seen = term.last_id
    term.ws.close()  # "the network dropped"; psql keeps running meanwhile

    again = open_term(f"session={term.sid}&last={seen}")
    try:
        assert again.resumed
        out = again.run("select 'after the drop';")
        assert "after the drop" in out
        assert "before the drop" not in again.screen
    finally:
        again.close()
        term.exit_code = 0  # nothing left for the fixture to close


def test_bad_message_is_rejected(term: Term):
    term.ws.send('{"type": "resize", "cols": 99999, "rows": 10}')
    deadline = time.monotonic() + 5
    while not term.errors and time.monotonic() < deadline:
        term._recv(timeout=deadline - time.monotonic())
    assert term.errors
    assert PROMPT in term.run("select 1;")  # the session is still alive afterwards
