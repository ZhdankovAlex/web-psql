"""Тесты на запущенном стенде: настоящие gateway, worker, Redis и PostgreSQL.

Перед запуском поднимите стенд::

    docker compose up --build -d --wait
    python -m pytest tests/stand

Адрес стенда — переменная ``STAND_URL`` (по умолчанию ``http://localhost:8000``).
Если стенд не отвечает, тесты пропускаются; в CI переменная ``STAND_REQUIRED=1``
превращает пропуск в ошибку, чтобы незапущенный стенд не выглядел как зелёный прогон.
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
    """GET-запрос к стенду.

    :param path: путь, например ``/api/sessions``.
    :return: разобранный JSON.
    """
    with urllib.request.urlopen(STAND_URL + path, timeout=5) as resp:
        return json.load(resp)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Пропустить тесты стенда, если он не запущен (в CI — упасть)."""
    try:
        http_json("/healthz")
    except OSError as exc:
        if os.environ.get("STAND_REQUIRED") == "1":
            raise pytest.UsageError(f"стенд {STAND_URL} недоступен: {exc}") from exc
        skip = pytest.mark.skip(reason=f"стенд {STAND_URL} не запущен")
        for item in items:
            if "stand" in item.keywords:
                item.add_marker(skip)


class Term:
    """Терминал через WebSocket — как вкладка браузера, только из теста."""

    def __init__(self, ws: ClientConnection) -> None:
        """Обернуть подключение.

        :param ws: открытое WebSocket-подключение.
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
        """Получить одно сообщение и запомнить вывод.

        :param timeout: сколько ждать, секунд.
        :return: сообщение.
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
        """Ждать, пока на экране появится текст.

        :param text: что ищем в выводе.
        :param timeout: сколько ждать, секунд.
        :return: весь вывод на этот момент.
        """
        deadline = time.monotonic() + timeout
        while text not in self.screen:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"не дождались {text!r}, на экране:\n{self.screen[-2000:]}")
            if self._recv(timeout=left)["type"] == "exit":
                raise AssertionError(f"psql завершился, не дождавшись {text!r}:\n{self.screen[-2000:]}")
        return self.screen

    def run(self, command: str, timeout: float = 10) -> str:
        """Выполнить команду и вернуть её вывод (между командой и следующим приглашением).

        :param command: строка для psql.
        :param timeout: сколько ждать, секунд.
        :return: вывод команды.
        """
        start = len(self.screen)
        self.ws.send(json.dumps({"type": "input", "data": command + "\r"}))
        deadline = time.monotonic() + timeout
        while self.screen.count(PROMPT, start) < 1:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"нет приглашения после {command!r}:\n{self.screen[start:]}")
            self._recv(timeout=left)
        return self.screen[start:]

    def close(self, timeout: float = 10) -> int | None:
        """Попросить worker завершить psql и дождаться кода выхода.

        :param timeout: сколько ждать, секунд.
        :return: код выхода psql.
        """
        self.ws.send(json.dumps({"type": "close"}))
        deadline = time.monotonic() + timeout
        while self.exit_code is None and time.monotonic() < deadline:
            self._recv(timeout=deadline - time.monotonic())
        return self.exit_code


def open_term(query: str = "") -> Term:
    """Открыть сессию и дождаться приглашения psql.

    :param query: параметры подключения, например ``session=…&last=…``.
    :return: терминал.
    """
    term = Term(connect(f"{WS_URL}?{query}", open_timeout=10))
    if not term.resumed:
        term.wait_for(PROMPT, timeout=20)
    return term


@pytest.fixture
def term() -> Iterator[Term]:
    """Новая сессия с чистой копией базы; закрывается после теста."""
    t = open_term("cols=200&rows=50")
    yield t
    if t.exit_code is None:
        t.close()
    t.ws.close()
