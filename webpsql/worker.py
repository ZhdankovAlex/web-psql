"""Worker: держит псевдотерминалы с psql и связывает их с Redis Streams.

Запуск: ``python -m webpsql.worker``.

Worker читает заявки на открытие сессий из ``term:control`` в составе группы читателей
Redis (consumer group): worker'ов можно запустить несколько, и каждая заявка достанется
ровно одному из них.
Дальше сессия живёт на этом worker'е: ввод он читает из ``term:{sid}:in``,
вывод терминала пишет в ``term:{sid}:out``.

Для каждой сессии worker создаёт отдельную копию учебной базы и отдельную роль
(:mod:`webpsql.sandbox`), а psql запускает от имени непривилегированного пользователя ОС
с чистым окружением — так через ``\\!`` в psql не добраться до паролей worker'а.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import logging
import os
import pty
import pwd
import resource
import signal
import socket
import struct
import subprocess
import termios
from datetime import UTC, datetime
from pathlib import Path

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from webpsql.config import Settings, settings
from webpsql.sandbox import Sandbox, SandboxManager
from webpsql.streams import (
    CONTROL_STREAM,
    OWNER_RENEW,
    OWNER_TTL,
    WORKERS_GROUP,
    Kind,
    SessionStatus,
    alive_key,
    input_stream,
    meta_key,
    output_stream,
    owner_key,
)

log = logging.getLogger("webpsql.worker")

READ_CHUNK = 64 * 1024
"""Сколько байт читать из pty за один раз."""

OUTPUT_TTL = 300
"""Сколько секунд хранить потоки сессии после её завершения."""

PSQLRC = Path(__file__).with_name("psqlrc")
"""Настройки psql в песочнице: приглашение ``bookstore=>`` вместо имени копии базы."""

CLEANUP_INTERVAL = 60
"""Как часто (в секундах) искать копии баз, оставшиеся от упавших worker'ов."""


def _clamp(value: str | int | None, default: int, low: int = 2, high: int = 1000) -> int:
    """Привести размер окна к разумному целому.

    :param value: значение из сообщения (строка или число).
    :param default: значение, если ``value`` пустое или не число.
    :param low: нижняя граница.
    :param high: верхняя граница.
    :return: число в диапазоне ``[low, high]``.
    """
    try:
        return max(low, min(high, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _now() -> str:
    """Текущее время в ISO 8601 (UTC) для ``term:{sid}:meta``."""
    return datetime.now(UTC).isoformat()


class Terminal:
    """Один psql в одном псевдотерминале, подключённый к своей копии базы.

    Вывод читается без ожидания: ``loop.add_reader`` вызывает чтение, только когда в pty
    появились данные. Прочитанное кладётся в очередь, а отдельная задача забирает из неё всё
    накопившееся и отправляет одним ``XADD`` — так при большом выводе (например, длинный
    ``SELECT``) в Redis уходит меньше сообщений.
    """

    def __init__(
        self, sid: str, redis: aioredis.Redis, cfg: Settings, sandboxes: SandboxManager,
        worker: str, cols: int, rows: int,
    ) -> None:
        """Подготовить сессию; копия базы и процесс создаются в :meth:`run`.

        :param sid: идентификатор сессии.
        :param redis: клиент Redis.
        :param cfg: настройки.
        :param sandboxes: кто создаёт и удаляет копии базы.
        :param worker: имя worker'а — записывается в сведения о сессии.
        :param cols: ширина терминала в символах.
        :param rows: высота терминала в строках.
        """
        self.sid = sid
        self.redis = redis
        self.cfg = cfg
        self.sandboxes = sandboxes
        self.worker = worker
        self.cols = cols
        self.rows = rows
        self.master_fd: int = -1
        self.proc: subprocess.Popen[bytes] | None = None
        self._chunks: asyncio.Queue[str | None] = asyncio.Queue()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    # --- процесс и pty -------------------------------------------------------

    def _spawn(self, sandbox: Sandbox) -> None:
        """Создать pty и запустить в нём psql, подключённый к копии базы.

        Дочерний процесс получает slave-сторону pty как stdin/stdout/stderr,
        начинает новый сеанс ОС (``setsid``) и делает этот pty своим
        управляющим терминалом — тогда Ctrl+C из браузера доходит до psql как SIGINT.

        Окружение psql собирается с нуля: в нём только данные роли сессии, а не
        переменные worker'а (пароль суперпользователя, адрес Redis). Если задан
        ``SANDBOX_USER``, psql работает от его имени и не может прочитать
        ``/proc/<pid>/environ`` worker'а.

        :param sandbox: куда подключаться.
        """
        master, slave = pty.openpty()
        self.master_fd = master
        self._set_winsize()

        user = pwd.getpwnam(self.cfg.sandbox_user) if self.cfg.sandbox_user else None
        if user is not None:
            # less и другие программы открывают /dev/tty — терминал должен принадлежать пользователю
            os.fchown(slave, user.pw_uid, user.pw_gid)
        nproc = self.cfg.sandbox_nproc

        def child_setup() -> None:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            if user is not None:
                resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))

        home = user.pw_dir if user else os.environ.get("HOME", "/tmp")
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": home,
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "PAGER": "less",
            "LESS": "-SRXF",  # длинные строки прокручиваются вбок, короткий вывод — без пейджера
            "PSQL_HISTORY": "/dev/null",  # файл истории был бы общим на всех — не сохраняем
            "PSQLRC": str(PSQLRC),
            "PGHOST": self.cfg.pg_host,
            "PGPORT": str(self.cfg.pg_port),
            "PGDATABASE": sandbox.database,
            "PGUSER": sandbox.role,
            "PGPASSWORD": sandbox.password,
            "PGAPPNAME": "web-psql",
        }
        self.proc = subprocess.Popen(  # noqa: S603 — команда из конфигурации, не от пользователя
            self.cfg.psql_args,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=home,
            start_new_session=True,
            user=user.pw_uid if user else None,
            group=user.pw_gid if user else None,
            extra_groups=[] if user else None,
            preexec_fn=child_setup,  # noqa: PLW1509 — в worker'е нет потоков
            close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)

    def _set_winsize(self) -> None:
        """Сообщить pty новый размер окна; ядро пришлёт psql сигнал SIGWINCH."""
        if self.master_fd < 0:
            return
        packed = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)

    def _on_readable(self) -> None:
        """Вызывается, когда в pty появились данные (или он закрылся)."""
        try:
            data = os.read(self.master_fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:  # EIO — процесс завершился и закрыл slave
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.master_fd)
            self._chunks.put_nowait(None)
            return
        text = self._decoder.decode(data)
        if text:
            self._chunks.put_nowait(text)

    # --- задачи сессии -------------------------------------------------------

    async def _pump_output(self) -> None:
        """Отправлять вывод pty в поток ``term:{sid}:out``, объединяя накопившееся в одно сообщение."""
        out = output_stream(self.sid)
        while True:
            first = await self._chunks.get()
            parts: list[str] = []
            finished = first is None
            if first is not None:
                parts.append(first)
            while not finished and not self._chunks.empty():
                item = self._chunks.get_nowait()
                if item is None:
                    finished = True
                else:
                    parts.append(item)
            if parts:
                await self.redis.xadd(
                    out, {"kind": Kind.OUTPUT, "data": "".join(parts)},
                    maxlen=self.cfg.stream_maxlen, approximate=True,
                )
            if finished:
                return

    async def _consume_input(self) -> None:
        """Читать ``term:{sid}:in`` и передавать нажатия клавиш и размер окна в pty."""
        stream = input_stream(self.sid)
        last_id = "0-0"  # поток создаётся под сессию — читаем с самого начала
        while True:
            batches = await self.redis.xread({stream: last_id}, block=5000, count=100)
            for _, messages in batches:
                for msg_id, fields in messages:
                    last_id = msg_id
                    kind = fields.get("kind")
                    if kind == Kind.INPUT:
                        await self._write(fields.get("data", ""))
                    elif kind == Kind.RESIZE:
                        self.cols = _clamp(fields.get("cols"), self.cols)
                        self.rows = _clamp(fields.get("rows"), self.rows)
                        self._set_winsize()
                        await self.redis.hset(meta_key(self.sid), mapping={"cols": self.cols, "rows": self.rows})
                    elif kind == Kind.CLOSE:
                        log.info("session %s: закрыта клиентом", self.sid)
                        return

    async def _write(self, text: str) -> None:
        """Записать ввод пользователя в pty.

        :param text: символы из xterm.js (включая управляющие, например ``\\x03`` для Ctrl+C).
        """
        data = text.encode()
        while data:
            try:
                written = os.write(self.master_fd, data)
            except BlockingIOError:  # буфер pty полон (например, вставили много текста)
                await asyncio.sleep(0.01)
                continue
            except OSError:
                return
            data = data[written:]

    async def _hold_lease(self) -> None:
        """Обновлять отметку «worker жив», пока сессия работает.

        Если worker упадёт, отметка исчезнет через :data:`OWNER_TTL` секунд: gateway закроет
        сессию в браузере, а другой worker при уборке удалит её копию базы.
        """
        while True:
            await self.redis.set(owner_key(self.sid), self.worker, ex=OWNER_TTL)
            await asyncio.sleep(OWNER_RENEW)

    async def _watch_heartbeat(self) -> None:
        """Завершить сессию, если пропала отметка «вкладка открыта» (вкладку закрыли)."""
        while True:
            await asyncio.sleep(5)
            if not await self.redis.exists(alive_key(self.sid)):
                log.info("session %s: вкладка закрыта, завершаю", self.sid)
                return

    # --- жизненный цикл ------------------------------------------------------

    async def run(self) -> None:
        """Создать копию базы, запустить psql и обслуживать сессию до её завершения."""
        loop = asyncio.get_running_loop()
        # отметку ставим до создания базы, иначе уборка сочтёт новую копию забытой и удалит
        lease = asyncio.create_task(self._hold_lease())
        try:
            sandbox = await self.sandboxes.create(self.sid)
            self._spawn(sandbox)
        except Exception as exc:  # noqa: BLE001 — любую ошибку показываем пользователю
            log.exception("session %s: не удалось запустить", self.sid)
            await self._emit_error(f"не удалось открыть сессию: {exc}")
            await self._emit_exit(-1)
            lease.cancel()
            await self._cleanup()
            return

        await self.redis.xadd(output_stream(self.sid), {
            "kind": Kind.OUTPUT,
            "data": f"\x1b[90mСвоя копия базы bookstore готова за {sandbox.created_ms} мс. "
                    f"Меняйте и удаляйте что угодно: «Начать заново» вернёт исходные данные.\x1b[0m\r\n\r\n",
        })
        await self.redis.hset(meta_key(self.sid), mapping={
            "status": SessionStatus.RUNNING, "worker": self.worker,
            "pid": self.proc.pid if self.proc else "", "cols": self.cols, "rows": self.rows,
            "database": sandbox.database, "sandbox_ms": sandbox.created_ms,
        })
        loop.add_reader(self.master_fd, self._on_readable)
        pump = asyncio.create_task(self._pump_output())
        stoppers = [
            asyncio.create_task(self._consume_input()),
            asyncio.create_task(self._watch_heartbeat()),
        ]
        log.info("session %s: psql pid=%s, база %s", self.sid, self.proc.pid if self.proc else "?", sandbox.database)
        await asyncio.wait([pump, *stoppers], return_when=asyncio.FIRST_COMPLETED)

        for task in stoppers:
            task.cancel()
        code = await self._terminate()
        # psql завершён — отправляем остаток вывода, а потом сообщаем о выходе
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(pump, timeout=2)
        pump.cancel()
        await self._emit_exit(code)
        lease.cancel()
        await self._cleanup()

    async def _terminate(self) -> int:
        """Остановить psql: SIGHUP группе процессов, затем SIGKILL, если не помогло.

        :return: код выхода процесса (отрицательный — номер сигнала).
        """
        proc = self.proc
        if proc is None:
            return -1
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGHUP)
            for _ in range(20):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
        return await asyncio.to_thread(proc.wait)

    async def _emit_exit(self, code: int) -> None:
        """Сообщить gateway, что терминал завершился.

        :param code: код выхода psql.
        """
        await self.redis.xadd(output_stream(self.sid), {"kind": Kind.EXIT, "code": str(code)})
        await self.redis.hset(meta_key(self.sid), mapping={
            "status": SessionStatus.EXITED, "exit_code": code, "finished_at": _now(),
        })

    async def _emit_error(self, message: str) -> None:
        """Отправить gateway текст ошибки.

        :param message: человекочитаемое описание.
        """
        await self.redis.xadd(output_stream(self.sid), {"kind": Kind.ERROR, "message": message})

    async def _cleanup(self) -> None:
        """Закрыть pty, удалить копию базы, потоки сессии оставить ещё на :data:`OUTPUT_TTL` секунд."""
        if self.master_fd >= 0:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self.master_fd)
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
        try:
            await self.sandboxes.drop(self.sid)
        except Exception:  # noqa: BLE001 — копию удалит фоновая уборка
            log.exception("session %s: не удалось удалить песочницу", self.sid)
        await self.redis.delete(input_stream(self.sid), alive_key(self.sid), owner_key(self.sid))
        await self.redis.expire(output_stream(self.sid), OUTPUT_TTL)
        await self.redis.expire(meta_key(self.sid), OUTPUT_TTL)


class Worker:
    """Принимает заявки на новые сессии и запускает :class:`Terminal`."""

    def __init__(self, cfg: Settings) -> None:
        """Создать worker.

        :param cfg: настройки.
        """
        self.cfg = cfg
        # socket_timeout=None: XREAD BLOCK держит соединение дольше любого разумного таймаута
        self.redis = aioredis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=None)
        self.name = f"{socket.gethostname()}-{os.getpid()}"
        self.sessions: dict[str, asyncio.Task[None]] = {}
        self.sandboxes = SandboxManager(cfg.admin_dsn)

    async def _ensure_group(self) -> None:
        """Создать группу читателей для ``term:control``, если её ещё нет."""
        try:
            await self.redis.xgroup_create(CONTROL_STREAM, WORKERS_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _open(self, sid: str, cols: int, rows: int) -> None:
        """Открыть сессию или отказать, если достигнут лимит.

        :param sid: идентификатор сессии.
        :param cols: ширина терминала.
        :param rows: высота терминала.
        """
        if len(self.sessions) >= self.cfg.max_sessions:
            out = output_stream(sid)
            await self.redis.xadd(out, {"kind": Kind.ERROR, "message": "слишком много открытых терминалов"})
            await self.redis.xadd(out, {"kind": Kind.EXIT, "code": "-1"})
            await self.redis.hset(meta_key(sid), mapping={
                "status": SessionStatus.REJECTED, "worker": self.name, "finished_at": _now(),
            })
            await self.redis.expire(out, OUTPUT_TTL)
            await self.redis.expire(meta_key(sid), OUTPUT_TTL)
            return
        terminal = Terminal(sid, self.redis, self.cfg, self.sandboxes, self.name, cols, rows)
        task = asyncio.create_task(terminal.run(), name=f"session-{sid}")
        self.sessions[sid] = task
        task.add_done_callback(lambda _t, s=sid: self.sessions.pop(s, None))

    async def _cleanup_forgotten(self) -> None:
        """Фоновая уборка: удалять копии баз, у которых не осталось работающей сессии.

        Обычно копию удаляет сам :class:`Terminal`, но если worker упал или его
        перезапустили, базы и пользователи остаются. Копия считается забытой, если её
        сессия не на этом worker'е и отметку «worker жив» для неё никто не обновляет.
        """
        while True:
            try:
                for sid in await self.sandboxes.list_sids():
                    if sid not in self.sessions and not await self.redis.exists(owner_key(sid)):
                        log.info("уборка: удаляю забытую копию базы %s", sid)
                        await self.sandboxes.drop(sid)
            except Exception:  # noqa: BLE001 — ошибка уборки не должна ронять worker
                log.exception("уборка: ошибка")
            await asyncio.sleep(CLEANUP_INTERVAL)

    async def run(self) -> None:
        """Главный цикл: читать ``term:control`` и открывать сессии."""
        await self.sandboxes.start()
        cleanup_task = asyncio.create_task(self._cleanup_forgotten(), name="sandbox-cleanup")
        await self._ensure_group()
        log.info("worker %s готов, лимит сессий: %d", self.name, self.cfg.max_sessions)
        try:
            while True:
                batches = await self.redis.xreadgroup(
                    WORKERS_GROUP, self.name, {CONTROL_STREAM: ">"}, count=10, block=5000,
                )
                for _, messages in batches:
                    for msg_id, fields in messages:
                        if fields.get("kind") == Kind.OPEN and fields.get("sid"):
                            await self._open(
                                fields["sid"],
                                _clamp(fields.get("cols"), 80),
                                _clamp(fields.get("rows"), 24),
                            )
                        await self.redis.xack(CONTROL_STREAM, WORKERS_GROUP, msg_id)
        finally:
            cleanup_task.cancel()
            await self.sandboxes.close()


def main() -> None:
    """Точка входа ``python -m webpsql.worker``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Worker(settings).run())


if __name__ == "__main__":
    main()
