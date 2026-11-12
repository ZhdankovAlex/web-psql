"""Настройки из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    """Прочитать целое число из окружения.

    :param name: имя переменной.
    :param default: значение, если переменная не задана.
    :return: значение переменной или ``default``.
    """
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    """Общие настройки gateway и worker.

    :ivar redis_url: адрес Redis.
    :ivar psql_args: команда, которая запускается в каждом терминале.
    :ivar max_sessions: сколько терминалов одновременно держит один worker.
    :ivar heartbeat_ttl: сколько секунд ждать браузер после обрыва связи; потом psql завершается.
    :ivar stream_maxlen: сколько последних кусков вывода хранить в потоке (для переподключения).
    :ivar admin_dsn: подключение суперпользователя — worker создаёт им песочницы.
    :ivar pg_host: адрес PostgreSQL для psql.
    :ivar pg_port: порт PostgreSQL для psql.
    :ivar sandbox_user: пользователь ОС, от имени которого запускается psql
        (``None`` — от имени самого worker'а; так удобно при локальной разработке).
    :ivar sandbox_nproc: сколько процессов одновременно может быть у этого пользователя
        (защита от fork-бомбы через ``\\!`` в psql).
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
