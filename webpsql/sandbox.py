"""Sandbox: a separate copy of the demo database and a separate database user for every session.

The copy is made with ``CREATE DATABASE … TEMPLATE bookstore_template`` — PostgreSQL copies
the template, which takes tens of milliseconds for a database of a few megabytes.
When the session ends, the database and the user are dropped, so the user can do anything
(``DROP TABLE``, ``TRUNCATE``, ``UPDATE`` without ``WHERE``) — the next session starts
with the original data again.

What the session user can do:

* it is a member of ``sandbox_owner``, the owner of every object in the template, so it can
  change and drop any table in its own copy;
* it owns its own database; all other databases are closed to it (``REVOKE CONNECT … FROM PUBLIC``);
* it is not a superuser: it cannot create databases or users, read server files or run
  programs through ``COPY … PROGRAM``;
* per-session limits: ``statement_timeout``, ``temp_file_limit``, ``CONNECTION LIMIT``.
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
"""Settings applied to the session user (``ALTER ROLE … SET``)."""


@dataclass(frozen=True)
class Sandbox:
    """Details psql needs to connect to the database copy.

    :ivar database: name of the session database.
    :ivar role: name of the session user.
    :ivar password: the user's password (lives as long as the session).
    :ivar created_ms: how many milliseconds it took to create the copy.
    """

    database: str
    role: str
    password: str
    created_ms: int


def _names(sid: str) -> tuple[str, str]:
    """Database and user names for a session.

    The names go straight into DDL (it does not support parameters), so the session id
    is checked against a strict pattern.

    :param sid: session id (32 hex characters).
    :return: ``(database, role)``.
    :raises ValueError: if ``sid`` does not look like a session id.
    """
    if not _SID_RE.match(sid):
        raise ValueError(f"invalid session id: {sid!r}")
    return DB_PREFIX + sid, ROLE_PREFIX + sid


class SandboxManager:
    """Creates and drops sandboxes through an administrator connection to PostgreSQL."""

    def __init__(self, admin_dsn: str) -> None:
        """Remember where to connect; the pool is created in :meth:`start`.

        :param admin_dsn: superuser DSN, database ``postgres``.
        """
        self.admin_dsn = admin_dsn
        self.pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        """Open the pool of administrator connections."""
        self.pool = await asyncpg.create_pool(self.admin_dsn, min_size=1, max_size=4)

    async def close(self) -> None:
        """Close the pool."""
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        """The open connection pool.

        :raises RuntimeError: if :meth:`start` has not been called yet.
        """
        if self.pool is None:
            raise RuntimeError("SandboxManager.start() was not called")
        return self.pool

    async def create(self, sid: str) -> Sandbox:
        """Create a user and a copy of the demo database for a session.

        :param sid: session id.
        :return: details psql needs to connect.
        """
        database, role = _names(sid)
        password = secrets.token_urlsafe(24)
        started = time.perf_counter()
        async with self._pool().acquire() as conn:
            # the password is a literal in DDL too; token_urlsafe only produces [A-Za-z0-9_-]
            await conn.execute(
                f"create role {role} login password '{password}' connection limit 3 in role {OWNER_ROLE}"
            )
            for name, value in ROLE_SETTINGS.items():
                await conn.execute(f"alter role {role} set {name} = '{value}'")
            # CREATE DATABASE cannot run inside a transaction — and asyncpg does not open one by itself
            await conn.execute(f"create database {database} template {TEMPLATE_DB} owner {role}")
            await conn.execute(f"revoke connect on database {database} from public")
        created_ms = round((time.perf_counter() - started) * 1000)
        log.info("sandbox %s: created in %d ms", database, created_ms)
        return Sandbox(database=database, role=role, password=password, created_ms=created_ms)

    async def drop(self, sid: str) -> None:
        """Drop the session's database copy and user (connections to the database are cut).

        :param sid: session id.
        """
        database, role = _names(sid)
        async with self._pool().acquire() as conn:
            await conn.execute(f"drop database if exists {database} with (force)")
            await conn.execute(f"drop role if exists {role}")
        log.info("sandbox %s: dropped", database)

    async def list_sids(self) -> set[str]:
        """Session ids that have a database or a user in PostgreSQL.

        :return: set of session ids.
        """
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(
                "select substr(datname, $1) as sid from pg_database where datname like $2 "
                "union select substr(rolname, $3) from pg_roles where rolname like $4",
                len(DB_PREFIX) + 1, DB_PREFIX + "%", len(ROLE_PREFIX) + 1, ROLE_PREFIX + "%",
            )
        return {r["sid"] for r in rows if _SID_RE.match(r["sid"])}
