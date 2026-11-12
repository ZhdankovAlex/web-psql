"""Имена ключей Redis и формат сообщений между gateway и worker.

Схема ключей::

    term:control          gateway -> worker   заявки на сессию        (consumer group "workers")
    term:{sid}:in         gateway -> worker   ввод, размер окна, закрытие
    term:{sid}:out        worker  -> gateway  вывод, ошибки, код выхода
    term:{sid}:alive      gateway             пульс браузера: ключ с TTL
    term:{sid}:owner      worker              аренда: worker жив и держит psql (ключ с TTL)
    term:{sid}:meta       gateway и worker    сведения о сессии (hash), см. :class:`SessionStatus`

Redis хранит поля сообщений строками; тип сообщения лежит в поле ``kind`` (:class:`Kind`).
"""

from __future__ import annotations

from enum import StrEnum

CONTROL_STREAM = "term:control"
WORKERS_GROUP = "workers"
META_PATTERN = "term:*:meta"

OWNER_TTL = 15
"""Через сколько секунд без продления аренды сессия считается потерянной (worker упал)."""

OWNER_RENEW = 5
"""Как часто worker продлевает аренду."""

META_TTL = 24 * 3600
"""Страховочный TTL сведений о сессии: если worker упадёт, ключ всё равно исчезнет."""


class Kind(StrEnum):
    """Тип сообщения в потоках Redis (поле ``kind``)."""

    OPEN = "open"
    """``term:control``: открыть сессию (поля ``sid``, ``cols``, ``rows``)."""
    INPUT = "input"
    """``term:{sid}:in``: нажатия клавиш (``data``)."""
    RESIZE = "resize"
    """``term:{sid}:in``: новый размер окна (``cols``, ``rows``)."""
    CLOSE = "close"
    """``term:{sid}:in``: завершить psql."""
    OUTPUT = "output"
    """``term:{sid}:out``: вывод терминала (``data``)."""
    ERROR = "error"
    """``term:{sid}:out``: ошибка (``message``)."""
    EXIT = "exit"
    """``term:{sid}:out``: psql завершился (``code``)."""


class SessionStatus(StrEnum):
    """Состояние сессии в ``term:{sid}:meta``."""

    PENDING = "pending"
    """Gateway отправил заявку, worker её ещё не взял."""
    RUNNING = "running"
    """psql запущен."""
    REJECTED = "rejected"
    """Worker отказал: достигнут лимит терминалов."""
    EXITED = "exited"
    """psql завершился."""
    LOST = "lost"
    """Worker перестал продлевать аренду (упал или перезапущен) — psql и копия базы потеряны."""


def input_stream(sid: str) -> str:
    """Поток ввода сессии.

    :param sid: идентификатор сессии.
    :return: ключ Redis.
    """
    return f"term:{sid}:in"


def output_stream(sid: str) -> str:
    """Поток вывода сессии.

    :param sid: идентификатор сессии.
    :return: ключ Redis.
    """
    return f"term:{sid}:out"


def alive_key(sid: str) -> str:
    """Ключ-пульс: пока он есть, worker держит терминал.

    :param sid: идентификатор сессии.
    :return: ключ Redis.
    """
    return f"term:{sid}:alive"


def owner_key(sid: str) -> str:
    """Аренда сессии: пока ключ есть, worker с этим именем держит psql.

    :param sid: идентификатор сессии.
    :return: ключ Redis.
    """
    return f"term:{sid}:owner"


def meta_key(sid: str) -> str:
    """Hash со сведениями о сессии: статус, worker, время, число подключённых вкладок.

    :param sid: идентификатор сессии.
    :return: ключ Redis.
    """
    return f"term:{sid}:meta"
