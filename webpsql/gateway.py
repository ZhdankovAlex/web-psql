"""Gateway: web pages and the WebSocket between the browser and Redis Streams.

Run: ``uvicorn webpsql.gateway:app --host 0.0.0.0 --port 8000``.

The ``/ws`` WebSocket messages are described by the models in :mod:`webpsql.protocol`.
Connection parameters: ``?cols=&rows=`` for a new session,
``?session=<id>&last=<id>`` to come back to a running session and get the output you missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from webpsql.config import settings
from webpsql.protocol import (
    CloseMessage,
    ErrorMessage,
    ExitMessage,
    InputMessage,
    OutputMessage,
    ResizeMessage,
    ServerMessage,
    SessionInfo,
    SessionMessage,
    client_message,
)
from webpsql.streams import (
    CONTROL_STREAM,
    META_PATTERN,
    META_TTL,
    OWNER_RENEW,
    Kind,
    SessionStatus,
    alive_key,
    input_stream,
    meta_key,
    output_stream,
    owner_key,
)

log = logging.getLogger("webpsql.gateway")
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the Redis connection for the lifetime of the app.

    :param app: the FastAPI app.
    """
    # socket_timeout=None: XREAD BLOCK keeps the connection waiting longer than any sane timeout
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=None)
    yield
    await app.state.redis.aclose()


app = FastAPI(
    title="web-psql",
    description="psql in the browser: WebSocket ↔ Redis Streams ↔ pseudo-terminal.\n\n"
                "The terminal itself works over the `/ws` WebSocket (Swagger does not show it); "
                "its messages are described in the README. This page lists the HTTP endpoints.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _redis() -> aioredis.Redis:
    """The Redis client opened in :func:`lifespan`."""
    return app.state.redis


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the terminal page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sessions", include_in_schema=False)
async def sessions_page() -> FileResponse:
    """Serve the sessions page."""
    return FileResponse(STATIC_DIR / "sessions.html")


@app.get("/healthz", summary="Health check", response_description="`{\"redis\": true}` if Redis answers")
async def healthz() -> dict[str, bool]:
    """Check that the gateway is up and can reach Redis."""
    return {"redis": bool(await _redis().ping())}


def _session_info(sid: str, meta: dict[str, str]) -> SessionInfo:
    """Build :class:`SessionInfo` from the ``term:{sid}:meta`` hash.

    :param sid: session id.
    :param meta: hash fields (empty strings mean "no value").
    :return: session details.
    """
    return SessionInfo(id=sid, **{k: v for k, v in meta.items() if v != ""})


@app.get(
    "/api/sessions",
    summary="Terminal sessions",
    response_description="Sessions, newest first. Finished ones stay visible for 5 more minutes.",
)
async def list_sessions() -> list[SessionInfo]:
    """List terminal sessions: which worker runs psql and whether a browser is connected."""
    redis = _redis()
    sessions = []
    async for key in redis.scan_iter(match=META_PATTERN, count=100):
        meta = await redis.hgetall(key)
        if meta:
            sessions.append(_session_info(key.split(":")[1], meta))
    return sorted(sessions, key=lambda s: s.created_at, reverse=True)


@app.get(
    "/api/sessions/{sid}",
    summary="One session",
    responses={404: {"description": "No such session, or it finished more than 5 minutes ago"}},
)
async def get_session(sid: str) -> SessionInfo:
    """Show the details of one session.

    :param sid: session id.
    """
    meta = await _redis().hgetall(meta_key(sid))
    if not meta:
        raise HTTPException(status_code=404, detail="session not found")
    return _session_info(sid, meta)


class Session:
    """One WebSocket connection attached to a terminal session."""

    def __init__(self, ws: WebSocket, redis: aioredis.Redis, sid: str, last_id: str) -> None:
        """Attach the WebSocket to a session.

        :param ws: the accepted WebSocket.
        :param redis: Redis client.
        :param sid: session id.
        :param last_id: where to start reading the output stream (``0-0`` — from the beginning).
        """
        self.ws = ws
        self.redis = redis
        self.sid = sid
        self.last_id = last_id

    async def send(self, message: ServerMessage) -> None:
        """Send a message to the browser.

        :param message: one of the server → browser models.
        """
        await self.ws.send_text(message.model_dump_json())

    async def heartbeat(self) -> None:
        """Keep the "tab is open" mark fresh while the browser is connected."""
        ttl = settings.heartbeat_ttl
        while True:
            await self.redis.set(alive_key(self.sid), "1", ex=ttl)
            await asyncio.sleep(max(1, ttl // 3))

    async def watch_worker(self) -> None:
        """End the session if the worker stopped refreshing its "worker is alive" mark (crashed or restarted)."""
        while True:
            await asyncio.sleep(OWNER_RENEW)
            status = await self.redis.hget(meta_key(self.sid), "status")
            if status == SessionStatus.RUNNING and not await self.redis.exists(owner_key(self.sid)):
                log.warning("session %s: the worker is gone", self.sid)
                await self.redis.hset(meta_key(self.sid), mapping={
                    "status": SessionStatus.LOST, "finished_at": datetime.now(UTC).isoformat(),
                })
                await self.redis.expire(meta_key(self.sid), 300)
                await self.redis.delete(alive_key(self.sid))
                await self.send(ErrorMessage(message="the worker running this psql restarted — the session is lost"))
                await self.send(ExitMessage(code=-1))
                return

    async def browser_to_redis(self) -> None:
        """Check the browser's messages and pass them on to the session input stream."""
        stream = input_stream(self.sid)
        while True:
            try:
                msg = client_message.validate_json(await self.ws.receive_text())
            except ValidationError as exc:
                await self.send(ErrorMessage(message=f"invalid message: {exc.errors()[0]['msg']}"))
                continue
            match msg:
                case InputMessage(data=data):
                    await self.redis.xadd(stream, {"kind": Kind.INPUT, "data": data})
                case ResizeMessage(cols=cols, rows=rows):
                    await self.redis.xadd(stream, {"kind": Kind.RESIZE, "cols": cols, "rows": rows})
                case CloseMessage():
                    await self.redis.xadd(stream, {"kind": Kind.CLOSE})

    async def redis_to_browser(self) -> None:
        """Read the session output stream and send it to the browser until psql exits."""
        stream = output_stream(self.sid)
        while True:
            batches = await self.redis.xread({stream: self.last_id}, block=5000, count=200)
            for _, messages in batches:
                for msg_id, fields in messages:
                    self.last_id = msg_id
                    match fields.get("kind"):
                        case Kind.OUTPUT:
                            await self.send(OutputMessage(data=fields.get("data", ""), id=msg_id))
                        case Kind.ERROR:
                            await self.send(ErrorMessage(message=fields.get("message", "")))
                        case Kind.EXIT:
                            await self.send(ExitMessage(code=int(fields.get("code", -1))))
                            return


async def _open_session(redis: aioredis.Redis, cols: str, rows: str) -> str:
    """Create a new session and send the workers a request for it.

    :param redis: Redis client.
    :param cols: terminal width from the connection parameters.
    :param rows: terminal height from the connection parameters.
    :return: the new session id.
    """
    sid = uuid.uuid4().hex
    # set the "tab is open" mark before the request, otherwise the worker would end the session right away
    await redis.set(alive_key(sid), "1", ex=settings.heartbeat_ttl)
    await redis.hset(meta_key(sid), mapping={
        "status": SessionStatus.PENDING, "connections": 0,
        "cols": cols, "rows": rows, "created_at": datetime.now(UTC).isoformat(),
    })
    await redis.expire(meta_key(sid), META_TTL)
    await redis.xadd(CONTROL_STREAM, {"kind": Kind.OPEN, "sid": sid, "cols": cols, "rows": rows})
    return sid


@app.websocket("/ws")
async def terminal_ws(ws: WebSocket) -> None:
    """Connect the browser to a new or a running terminal session.

    :param ws: WebSocket from the browser.
    """
    await ws.accept()
    redis = _redis()
    params = ws.query_params

    sid = params.get("session", "")
    resumed = bool(sid) and bool(await redis.exists(alive_key(sid)))
    if resumed:
        last_id = params.get("last") or "0-0"
    else:
        sid = await _open_session(redis, params.get("cols", "80"), params.get("rows", "24"))
        last_id = "0-0"

    log.info("ws: session %s (%s)", sid, "resumed" if resumed else "new")
    session = Session(ws, redis, sid, last_id)
    await redis.hincrby(meta_key(sid), "connections", 1)
    tasks: list[asyncio.Task[None]] = []
    try:
        await session.send(SessionMessage(id=sid, resumed=resumed))
        tasks = [
            asyncio.create_task(session.heartbeat()),
            asyncio.create_task(session.browser_to_redis()),
            asyncio.create_task(session.redis_to_browser()),
            asyncio.create_task(session.watch_worker()),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await task
        if await redis.exists(meta_key(sid)):
            await redis.hincrby(meta_key(sid), "connections", -1)
        with contextlib.suppress(Exception):
            await ws.close()
    # If the browser just disconnected, psql keeps running for heartbeat_ttl more seconds —
    # the browser can reconnect with the same session id during that time.
