"""Redis key names and the message format between the gateway and the workers.

Keys::

    term:control          gateway -> worker   session requests; each goes to one worker (consumer group "workers")
    term:{sid}:in         gateway -> worker   input, window size, close
    term:{sid}:out        worker  -> gateway  output, errors, exit code
    term:{sid}:alive      gateway             "tab is open" mark (key with a TTL)
    term:{sid}:owner      worker              "worker is alive and runs psql" mark (key with a TTL)
    term:{sid}:meta       gateway and worker  session details (hash), see :class:`SessionStatus`

Redis stores message fields as strings; the message type is in the ``kind`` field (:class:`Kind`).
"""

from __future__ import annotations

from enum import StrEnum

CONTROL_STREAM = "term:control"
WORKERS_GROUP = "workers"
META_PATTERN = "term:*:meta"

OWNER_TTL = 15
"""How many seconds the "worker is alive" mark lives: if the worker crashes, the session is lost after this."""

OWNER_RENEW = 5
"""How often the worker refreshes the "worker is alive" mark."""

META_TTL = 24 * 3600
"""Safety TTL for session details: even if the worker crashes, the key goes away eventually."""


class Kind(StrEnum):
    """Message type in the Redis streams (the ``kind`` field)."""

    OPEN = "open"
    """``term:control``: open a session (fields ``sid``, ``cols``, ``rows``)."""
    INPUT = "input"
    """``term:{sid}:in``: key presses (``data``)."""
    RESIZE = "resize"
    """``term:{sid}:in``: new window size (``cols``, ``rows``)."""
    CLOSE = "close"
    """``term:{sid}:in``: stop psql."""
    OUTPUT = "output"
    """``term:{sid}:out``: terminal output (``data``)."""
    ERROR = "error"
    """``term:{sid}:out``: an error (``message``)."""
    EXIT = "exit"
    """``term:{sid}:out``: psql has exited (``code``)."""


class SessionStatus(StrEnum):
    """Session state in ``term:{sid}:meta``."""

    PENDING = "pending"
    """The gateway sent a request, no worker has picked it up yet."""
    RUNNING = "running"
    """psql is running."""
    REJECTED = "rejected"
    """The worker refused: the terminal limit is reached."""
    EXITED = "exited"
    """psql has exited."""
    LOST = "lost"
    """The worker stopped refreshing its "worker is alive" mark (crashed or restarted) — psql and the copy are gone."""


def input_stream(sid: str) -> str:
    """The session input stream.

    :param sid: session id.
    :return: Redis key.
    """
    return f"term:{sid}:in"


def output_stream(sid: str) -> str:
    """The session output stream.

    :param sid: session id.
    :return: Redis key.
    """
    return f"term:{sid}:out"


def alive_key(sid: str) -> str:
    """The "tab is open" mark: the gateway refreshes it while a browser is connected; without it the worker ends psql.

    :param sid: session id.
    :return: Redis key.
    """
    return f"term:{sid}:alive"


def owner_key(sid: str) -> str:
    """The "worker is alive" mark: while the key exists, the worker with this name runs the session's psql.

    :param sid: session id.
    :return: Redis key.
    """
    return f"term:{sid}:owner"


def meta_key(sid: str) -> str:
    """Hash with session details: status, worker, timestamps, number of connected tabs.

    :param sid: session id.
    :return: Redis key.
    """
    return f"term:{sid}:meta"
