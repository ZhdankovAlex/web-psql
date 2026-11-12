"""Worker: runs psql in pseudo-terminals and connects them to Redis Streams.

Run: ``python -m webpsql.worker``.

The worker reads requests for new sessions from ``term:control`` as a member of a Redis
consumer group: you can run several workers, and each request goes to exactly one of them.
From then on the session lives on that worker: it reads input from ``term:{sid}:in``
and writes terminal output to ``term:{sid}:out``.

For each session the worker creates a separate copy of the demo database and a separate
database user (:mod:`webpsql.sandbox`), and runs psql as an unprivileged OS user with a
clean environment — so ``\\!`` in psql cannot reach the worker's passwords.
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
"""How many bytes to read from the pty at once."""

OUTPUT_TTL = 300
"""How many seconds to keep the session streams after the session ends."""

PSQLRC = Path(__file__).with_name("psqlrc")
"""psql settings for the sandbox: the ``bookstore=>`` prompt instead of the copy's real name."""

CLEANUP_INTERVAL = 60
"""How often (in seconds) to look for database copies left behind by crashed workers."""


def _clamp(value: str | int | None, default: int, low: int = 2, high: int = 1000) -> int:
    """Turn a window size into a sensible integer.

    :param value: value from the message (string or number).
    :param default: used when ``value`` is empty or not a number.
    :param low: lower bound.
    :param high: upper bound.
    :return: a number in ``[low, high]``.
    """
    try:
        return max(low, min(high, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _now() -> str:
    """Current time in ISO 8601 (UTC) for ``term:{sid}:meta``."""
    return datetime.now(UTC).isoformat()


class Terminal:
    """One psql in one pseudo-terminal, connected to its own copy of the database.

    Output is read without waiting: ``loop.add_reader`` triggers a read only when the pty
    has data. What was read goes into a queue, and a separate task takes everything that
    has piled up and sends it with one ``XADD`` — so a large output (say, a long ``SELECT``)
    produces fewer Redis messages.
    """

    def __init__(
        self, sid: str, redis: aioredis.Redis, cfg: Settings, sandboxes: SandboxManager,
        worker: str, cols: int, rows: int,
    ) -> None:
        """Prepare the session; the database copy and the process are created in :meth:`run`.

        :param sid: session id.
        :param redis: Redis client.
        :param cfg: settings.
        :param sandboxes: creates and drops database copies.
        :param worker: worker name — stored in the session details.
        :param cols: terminal width in characters.
        :param rows: terminal height in lines.
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

    # --- process and pty -----------------------------------------------------

    def _spawn(self, sandbox: Sandbox) -> None:
        """Create a pty and start psql in it, connected to the database copy.

        The child gets the slave side of the pty as stdin/stdout/stderr, starts a new
        OS session (``setsid``) and makes this pty its controlling terminal — that is how
        Ctrl+C from the browser reaches psql as SIGINT.

        The psql environment is built from scratch: it holds only the session's own
        connection details, none of the worker's variables (superuser password, Redis
        address). If ``SANDBOX_USER`` is set, psql runs as that user and cannot read the
        worker's ``/proc/<pid>/environ``.

        :param sandbox: where to connect.
        """
        master, slave = pty.openpty()
        self.master_fd = master
        self._set_winsize()

        user = pwd.getpwnam(self.cfg.sandbox_user) if self.cfg.sandbox_user else None
        if user is not None:
            # less and other programs open /dev/tty — the terminal must belong to the user
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
            "LESS": "-SRXF",  # long lines scroll sideways, short output skips the pager
            "PSQL_HISTORY": "/dev/null",  # the history file would be shared by everyone — do not keep it
            "PSQLRC": str(PSQLRC),
            "PGHOST": self.cfg.pg_host,
            "PGPORT": str(self.cfg.pg_port),
            "PGDATABASE": sandbox.database,
            "PGUSER": sandbox.role,
            "PGPASSWORD": sandbox.password,
            "PGAPPNAME": "web-psql",
        }
        self.proc = subprocess.Popen(  # noqa: S603 — the command comes from settings, not from the user
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
            preexec_fn=child_setup,  # noqa: PLW1509 — the worker has no threads
            close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)

    def _set_winsize(self) -> None:
        """Tell the pty the new window size; the kernel sends psql SIGWINCH."""
        if self.master_fd < 0:
            return
        packed = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)

    def _on_readable(self) -> None:
        """Called when the pty has data (or was closed)."""
        try:
            data = os.read(self.master_fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:  # EIO — the process exited and closed the slave side
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.master_fd)
            self._chunks.put_nowait(None)
            return
        text = self._decoder.decode(data)
        if text:
            self._chunks.put_nowait(text)

    # --- session tasks -------------------------------------------------------

    async def _pump_output(self) -> None:
        """Send pty output to the ``term:{sid}:out`` stream, merging whatever piled up into one message."""
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
        """Read ``term:{sid}:in`` and pass key presses and window size to the pty."""
        stream = input_stream(self.sid)
        last_id = "0-0"  # the stream is created for this session — read from the very beginning
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
                        log.info("session %s: closed by the client", self.sid)
                        return

    async def _write(self, text: str) -> None:
        """Write user input to the pty.

        :param text: characters from xterm.js (including control ones, e.g. ``\\x03`` for Ctrl+C).
        """
        data = text.encode()
        while data:
            try:
                written = os.write(self.master_fd, data)
            except BlockingIOError:  # the pty buffer is full (e.g. a lot of text was pasted)
                await asyncio.sleep(0.01)
                continue
            except OSError:
                return
            data = data[written:]

    async def _keep_alive_mark(self) -> None:
        """Keep the "worker is alive" mark fresh while the session runs.

        If the worker crashes, the mark disappears after :data:`OWNER_TTL` seconds: the gateway
        ends the session in the browser, and another worker's cleanup drops the database copy.
        """
        while True:
            await self.redis.set(owner_key(self.sid), self.worker, ex=OWNER_TTL)
            await asyncio.sleep(OWNER_RENEW)

    async def _watch_tab(self) -> None:
        """End the session when the "tab is open" mark disappears (the tab was closed)."""
        while True:
            await asyncio.sleep(5)
            if not await self.redis.exists(alive_key(self.sid)):
                log.info("session %s: the tab is closed, ending", self.sid)
                return

    # --- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Create the database copy, start psql and serve the session until it ends."""
        loop = asyncio.get_running_loop()
        # set the mark before creating the database, otherwise cleanup would treat the new copy as forgotten
        alive_mark = asyncio.create_task(self._keep_alive_mark())
        try:
            sandbox = await self.sandboxes.create(self.sid)
            self._spawn(sandbox)
        except Exception as exc:  # noqa: BLE001 — show any error to the user
            log.exception("session %s: failed to start", self.sid)
            await self._emit_error(f"could not open the session: {exc}")
            await self._emit_exit(-1)
            alive_mark.cancel()
            await self._cleanup()
            return

        await self.redis.xadd(output_stream(self.sid), {
            "kind": Kind.OUTPUT,
            "data": f"\x1b[90mYour own copy of the bookstore database is ready in {sandbox.created_ms} ms. "
                    f"Change or drop anything: “Start over” brings the original data back.\x1b[0m\r\n\r\n",
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
            asyncio.create_task(self._watch_tab()),
        ]
        log.info("session %s: psql pid=%s, database %s", self.sid, self.proc.pid if self.proc else "?",
                 sandbox.database)
        await asyncio.wait([pump, *stoppers], return_when=asyncio.FIRST_COMPLETED)

        for task in stoppers:
            task.cancel()
        code = await self._terminate()
        # psql has exited — send the rest of the output first, then report the exit
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(pump, timeout=2)
        pump.cancel()
        await self._emit_exit(code)
        alive_mark.cancel()
        await self._cleanup()

    async def _terminate(self) -> int:
        """Stop psql: SIGHUP to the process group, then SIGKILL if that did not help.

        :return: the process exit code (negative means the signal number).
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
        """Tell the gateway that the terminal has ended.

        :param code: psql exit code.
        """
        await self.redis.xadd(output_stream(self.sid), {"kind": Kind.EXIT, "code": str(code)})
        await self.redis.hset(meta_key(self.sid), mapping={
            "status": SessionStatus.EXITED, "exit_code": code, "finished_at": _now(),
        })

    async def _emit_error(self, message: str) -> None:
        """Send an error text to the gateway.

        :param message: human-readable description.
        """
        await self.redis.xadd(output_stream(self.sid), {"kind": Kind.ERROR, "message": message})

    async def _cleanup(self) -> None:
        """Close the pty, drop the database copy, keep the session streams for :data:`OUTPUT_TTL` more seconds."""
        if self.master_fd >= 0:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self.master_fd)
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
        try:
            await self.sandboxes.drop(self.sid)
        except Exception:  # noqa: BLE001 — the background cleanup will drop the copy
            log.exception("session %s: could not drop the database copy", self.sid)
        await self.redis.delete(input_stream(self.sid), alive_key(self.sid), owner_key(self.sid))
        await self.redis.expire(output_stream(self.sid), OUTPUT_TTL)
        await self.redis.expire(meta_key(self.sid), OUTPUT_TTL)


class Worker:
    """Takes requests for new sessions and starts a :class:`Terminal` for each."""

    def __init__(self, cfg: Settings) -> None:
        """Create the worker.

        :param cfg: settings.
        """
        self.cfg = cfg
        # socket_timeout=None: XREAD BLOCK keeps the connection waiting longer than any sane timeout
        self.redis = aioredis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=None)
        self.name = f"{socket.gethostname()}-{os.getpid()}"
        self.sessions: dict[str, asyncio.Task[None]] = {}
        self.sandboxes = SandboxManager(cfg.admin_dsn)

    async def _ensure_group(self) -> None:
        """Create the consumer group for ``term:control`` if it does not exist yet."""
        try:
            await self.redis.xgroup_create(CONTROL_STREAM, WORKERS_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _open(self, sid: str, cols: int, rows: int) -> None:
        """Open a session, or refuse if the limit is reached.

        :param sid: session id.
        :param cols: terminal width.
        :param rows: terminal height.
        """
        if len(self.sessions) >= self.cfg.max_sessions:
            out = output_stream(sid)
            await self.redis.xadd(out, {"kind": Kind.ERROR, "message": "too many open terminals, try again later"})
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
        """Background cleanup: drop database copies that no longer have a running session.

        Normally :class:`Terminal` drops its own copy, but if a worker crashed or was
        restarted, databases and users are left behind. A copy counts as forgotten when its
        session is not on this worker and nobody refreshes its "worker is alive" mark.
        """
        while True:
            try:
                for sid in await self.sandboxes.list_sids():
                    if sid not in self.sessions and not await self.redis.exists(owner_key(sid)):
                        log.info("cleanup: dropping forgotten database copy %s", sid)
                        await self.sandboxes.drop(sid)
            except Exception:  # noqa: BLE001 — a cleanup error must not bring the worker down
                log.exception("cleanup: error")
            await asyncio.sleep(CLEANUP_INTERVAL)

    async def run(self) -> None:
        """Main loop: read ``term:control`` and open sessions."""
        await self.sandboxes.start()
        cleanup_task = asyncio.create_task(self._cleanup_forgotten(), name="sandbox-cleanup")
        await self._ensure_group()
        log.info("worker %s ready, session limit: %d", self.name, self.cfg.max_sessions)
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
    """Entry point for ``python -m webpsql.worker``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Worker(settings).run())


if __name__ == "__main__":
    main()
