"""Tests against a running stack: the real gateway, worker, Redis and PostgreSQL.

Start the stack first::

    docker compose up --build -d --wait
    python -m pytest tests/stand

The stack address is the ``STAND_URL`` variable (default ``http://localhost:8000``).
If the stack does not answer, the tests are skipped; in CI ``STAND_REQUIRED=1`` turns
the skip into an error, so a stack that never started does not look like a green run.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections.abc import Iterator

import pytest
from websockets.sync.client import ClientConnection, connect

STAND_URL = os.environ.get("STAND_URL", "http://localhost:8000").rstrip("/")
WS_URL = STAND_URL.replace("http", "ws", 1) + "/ws"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROMPT = "bookstore=>"


def http_json(path: str) -> object:
    """GET request to the stack.

    :param path: path, e.g. ``/api/sessions``.
    :return: parsed JSON.
    """
    with urllib.request.urlopen(STAND_URL + path, timeout=5) as resp:
        return json.load(resp)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip the stack tests if the stack is not running (fail instead in CI)."""
    try:
        http_json("/healthz")
    except OSError as exc:
        if os.environ.get("STAND_REQUIRED") == "1":
            raise pytest.UsageError(f"stack {STAND_URL} is not reachable: {exc}") from exc
        skip = pytest.mark.skip(reason=f"stack {STAND_URL} is not running")
        for item in items:
            if "stand" in item.keywords:
                item.add_marker(skip)


class Term:
    """A terminal over WebSocket — like a browser tab, only driven by a test."""

    def __init__(self, ws: ClientConnection) -> None:
        """Wrap a connection.

        :param ws: an open WebSocket connection.
        """
        self.ws = ws
        self.screen = ""
        self.last_id = "0-0"
        self.exit_code: int | None = None
        self.errors: list[str] = []
        hello = self._recv(timeout=10)
        assert hello["type"] == "session", hello
        self.sid: str = hello["id"]
        self.resumed: bool = hello["resumed"]

    def _recv(self, timeout: float) -> dict:
        """Receive one message and keep the output.

        :param timeout: how long to wait, seconds.
        :return: the message.
        """
        msg = json.loads(self.ws.recv(timeout=timeout))
        if msg["type"] == "output":
            self.screen += ANSI.sub("", msg["data"]).replace("\r", "")
            self.last_id = msg["id"]
        elif msg["type"] == "error":
            self.errors.append(msg["message"])
        elif msg["type"] == "exit":
            self.exit_code = msg["code"]
        return msg

    def wait_for(self, text: str, timeout: float = 10) -> str:
        """Wait until the text shows up on the screen.

        :param text: what to look for in the output.
        :param timeout: how long to wait, seconds.
        :return: all output so far.
        """
        deadline = time.monotonic() + timeout
        while text not in self.screen:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"{text!r} never showed up, screen:\n{self.screen[-2000:]}")
            if self._recv(timeout=left)["type"] == "exit":
                raise AssertionError(f"psql exited before {text!r} showed up:\n{self.screen[-2000:]}")
        return self.screen

    def run(self, command: str, timeout: float = 10) -> str:
        """Run a command and return its output (between the command and the next prompt).

        :param command: a line for psql.
        :param timeout: how long to wait, seconds.
        :return: the command output.
        """
        start = len(self.screen)
        self.ws.send(json.dumps({"type": "input", "data": command + "\r"}))
        deadline = time.monotonic() + timeout
        while self.screen.count(PROMPT, start) < 1:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no prompt after {command!r}:\n{self.screen[start:]}")
            self._recv(timeout=left)
        return self.screen[start:]

    def close(self, timeout: float = 10) -> int | None:
        """Ask the worker to stop psql and wait for the exit code.

        :param timeout: how long to wait, seconds.
        :return: psql exit code.
        """
        self.ws.send(json.dumps({"type": "close"}))
        deadline = time.monotonic() + timeout
        while self.exit_code is None and time.monotonic() < deadline:
            self._recv(timeout=deadline - time.monotonic())
        return self.exit_code


def open_term(query: str = "") -> Term:
    """Open a session and wait for the psql prompt.

    :param query: connection parameters, e.g. ``session=…&last=…``.
    :return: the terminal.
    """
    term = Term(connect(f"{WS_URL}?{query}", open_timeout=10))
    if not term.resumed:
        term.wait_for(PROMPT, timeout=20)
    return term


@pytest.fixture
def term() -> Iterator[Term]:
    """A new session with a clean copy of the database; closed after the test."""
    t = open_term("cols=200&rows=50")
    yield t
    if t.exit_code is None:
        t.close()
    t.ws.close()
