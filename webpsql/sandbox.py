"""Песочница: отдельная копия учебной базы и отдельная роль на каждую сессию.

Копия создаётся командой ``CREATE DATABASE … TEMPLATE bookstore_template`` — PostgreSQL
копирует файлы шаблона, на базе в несколько мегабайт это десятки миллисекунд.
После завершения сессии база и роль удаляются, поэтому пользователь может делать
что угодно (``DROP TABLE``, ``TRUNCATE``, ``UPDATE`` без ``WHERE``) — следующая сессия
снова начнётся с исходных данных.

Права роли сессии:

* входит в ``sandbox_owner`` — владельца всех объектов шаблона, поэтому в своей копии
  может изменять и удалять любые таблицы;
* владелец своей базы, остальные базы ей закрыты (``REVOKE CONNECT … FROM PUBLIC``);
* не суперпользователь: не может создавать базы и роли, читать файлы сервера,
  запускать программы через ``COPY … PROGRAM``;
* ограничения на сессию: ``statement_timeout``, ``temp_file_limit``, ``CONNECTION LIMIT``.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass

import asyncpg

log = logging.getLogger("webpsql.sandbox")

TEMPLATE_DB = "bookstore_template"
OWNER_ROLE = "sandbox_owner"
DB_PREFIX = "sbx_"
ROLE_PREFIX = "s_"
_SID_RE = re.compile(r"^[0-9a-f]{32}$")

ROLE_SETTINGS = {
    "statement_timeout": "30s",
    "idle_in_transaction_session_timeout": "5min",
    "temp_file_limit": "256MB",
}
"""Настройки, которые ставятся роли сессии (``ALTER ROLE … SET``)."""


@dataclass(frozen=True)
class Sandbox:
    """Данные для подключения psql к копии базы.

    :ivar database: имя базы сессии.
    :ivar role: имя роли сессии.
    :ivar password: пароль роли (живёт столько же, сколько сессия).
    :ivar created_ms: сколько миллисекунд заняло создание копии.
    """

    database: str
    role: str
    password: str
    created_ms: int


def _names(sid: str) -> tuple[str, str]:
    """Имена базы и роли для сессии.

    Имена подставляются в DDL (параметры там не поддерживаются), поэтому
    идентификатор сессии проверяется по строгому шаблону.

    :param sid: идентификатор сессии (32 hex-символа).
    :return: ``(database, role)``.
    :raises ValueError: если ``sid`` не похож на идентификатор сессии.
    """
    if not _SID_RE.match(sid):
        raise ValueError(f"некорректный идентификатор сессии: {sid!r}")
    return DB_PREFIX + sid, ROLE_PREFIX + sid


class SandboxManager:
    """Создаёт и удаляет песочницы через административное подключение к PostgreSQL."""

    def __init__(self, admin_dsn: str) -> None:
        """Запомнить, куда подключаться; пул создаётся в :meth:`start`.

        :param admin_dsn: DSN суперпользователя, база ``postgres``.
        """
        self.admin_dsn = admin_dsn
        self.pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        """Открыть пул административных подключений."""
        self.pool = await asyncpg.create_pool(self.admin_dsn, min_size=1, max_size=4)

    async def close(self) -> None:
        """Закрыть пул."""
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        """Открытый пул подключений.

        :raises RuntimeError: если :meth:`start` ещё не вызывался.
        """
        if self.pool is None:
            raise RuntimeError("SandboxManager.start() не вызван")
        return self.pool

    async def create(self, sid: str) -> Sandbox:
        """Создать роль и копию учебной базы для сессии.

        :param sid: идентификатор сессии.
        :return: данные для подключения psql.
        """
        database, role = _names(sid)
        password = secrets.token_urlsafe(24)
        started = time.perf_counter()
        async with self._pool().acquire() as conn:
            # пароль — тоже литерал в DDL; token_urlsafe даёт только [A-Za-z0-9_-]
            await conn.execute(
                f"create role {role} login password '{password}' connection limit 3 in role {OWNER_ROLE}"
            )
            for name, value in ROLE_SETTINGS.items():
                await conn.execute(f"alter role {role} set {name} = '{value}'")
            # CREATE DATABASE нельзя выполнять в транзакции — asyncpg и не открывает её сам
            await conn.execute(f"create database {database} template {TEMPLATE_DB} owner {role}")
            await conn.execute(f"revoke connect on database {database} from public")
        created_ms = round((time.perf_counter() - started) * 1000)
        log.info("sandbox %s: создана за %d мс", database, created_ms)
        return Sandbox(database=database, role=role, password=password, created_ms=created_ms)

    async def drop(self, sid: str) -> None:
        """Удалить копию базы и роль сессии (подключения к базе обрываются).

        :param sid: идентификатор сессии.
        """
        database, role = _names(sid)
        async with self._pool().acquire() as conn:
            await conn.execute(f"drop database if exists {database} with (force)")
            await conn.execute(f"drop role if exists {role}")
        log.info("sandbox %s: удалена", database)

    async def list_sids(self) -> set[str]:
        """Идентификаторы сессий, для которых в PostgreSQL есть база или роль.

        :return: множество идентификаторов.
        """
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(
                "select substr(datname, $1) as sid from pg_database where datname like $2 "
                "union select substr(rolname, $3) from pg_roles where rolname like $4",
                len(DB_PREFIX) + 1, DB_PREFIX + "%", len(ROLE_PREFIX) + 1, ROLE_PREFIX + "%",
            )
        return {r["sid"] for r in rows if _SID_RE.match(r["sid"])}
