"""Settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    """Read an integer from the environment.

    :param name: variable name.
    :param default: value when the variable is not set.
    :return: the variable value or ``default``.
    """
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    """Settings shared by the gateway and the worker.

    :ivar redis_url: Redis address.
    :ivar psql_args: command started in every terminal.
    :ivar max_sessions: how many terminals one worker runs at the same time.
    :ivar heartbeat_ttl: how many seconds to wait for the browser after a dropped connection; then psql is stopped.
    :ivar stream_maxlen: how many recent pieces of output to keep in the stream (for reconnects).
    :ivar admin_dsn: superuser connection — the worker creates sandboxes with it.
    :ivar pg_host: PostgreSQL host for psql.
    :ivar pg_port: PostgreSQL port for psql.
    :ivar sandbox_user: OS user that runs psql
        (``None`` — the worker's own user; handy for local development).
    :ivar sandbox_nproc: how many processes that user may have at once
        (protection against a fork bomb through ``\\!`` in psql).
    """

    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    psql_args: tuple[str, ...] = field(
        default_factory=lambda: tuple(os.environ.get("PSQL_CMD", "psql").split())
    )
    max_sessions: int = field(default_factory=lambda: _env_int("MAX_SESSIONS", 10))
    heartbeat_ttl: int = field(default_factory=lambda: _env_int("HEARTBEAT_TTL", 60))
    stream_maxlen: int = field(default_factory=lambda: _env_int("STREAM_MAXLEN", 2000))
    admin_dsn: str = field(
        default_factory=lambda: os.environ.get("ADMIN_DSN", "postgresql://postgres@localhost/postgres")
    )
    pg_host: str = field(default_factory=lambda: os.environ.get("PG_HOST", "localhost"))
    pg_port: int = field(default_factory=lambda: _env_int("PG_PORT", 5432))
    sandbox_user: str | None = field(default_factory=lambda: os.environ.get("SANDBOX_USER") or None)
    sandbox_nproc: int = field(default_factory=lambda: _env_int("SANDBOX_NPROC", 256))


settings = Settings()
