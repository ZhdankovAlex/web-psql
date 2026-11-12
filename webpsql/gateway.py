"""Gateway: веб-страница и WebSocket между браузером и Redis Streams.

Запуск: ``uvicorn webpsql.gateway:app --host 0.0.0.0 --port 8000``.

Протокол WebSocket ``/ws`` описан моделями в :mod:`webpsql.protocol`.
Параметры подключения: ``?cols=&rows=`` для новой сессии,
``?session=<id>&last=<id>`` — вернуться к работающей сессии и получить пропущенный вывод.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

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
    """Открыть подключение к Redis на время работы приложения.

    :param app: приложение FastAPI.
    """
    # socket_timeout=None: XREAD BLOCK держит соединение дольше любого разумного таймаута
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=None)
    yield
    await app.state.redis.aclose()


app = FastAPI(
    title="web-psql",
    description="psql в браузере: WebSocket ↔ Redis Streams ↔ pty.\n\n"
                "Сам терминал работает через WebSocket `/ws` (Swagger его не показывает), "
                "протокол описан в README. Здесь — служебные HTTP-методы.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _redis() -> aioredis.Redis:
    """Клиент Redis, открытый в :func:`lifespan`."""
    return app.state.redis


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Отдать страницу с терминалом."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sessions", include_in_schema=False)
async def sessions_page() -> FileResponse:
    """Отдать страницу со списком сессий."""
    return FileResponse(STATIC_DIR / "sessions.html")


@app.get("/healthz", summary="Проверка живости", response_description="`{\"redis\": true}`, если Redis отвечает")
async def healthz() -> dict[str, bool]:
    """Проверить, что gateway работает и видит Redis."""
    return {"redis": bool(await _redis().ping())}


def _session_info(sid: str, meta: dict[str, str]) -> SessionInfo:
    """Собрать :class:`SessionInfo` из hash ``term:{sid}:meta``.

    :param sid: идентификатор сессии.
    :param meta: поля hash (пустые строки считаются отсутствующими).
    :return: сведения о сессии.
    """
    return SessionInfo(id=sid, **{k: v for k, v in meta.items() if v != ""})


@app.get(
    "/api/sessions",
    summary="Открытые сессии",
    response_description="Сессии, сначала новые. Завершённые видны ещё 5 минут.",
)
async def list_sessions() -> list[SessionInfo]:
    """Показать сессии терминала: на каком worker'е работает psql и подключён ли браузер."""
    redis = _redis()
    sessions = []
    async for key in redis.scan_iter(match=META_PATTERN, count=100):
        meta = await redis.hgetall(key)
        if meta:
            sessions.append(_session_info(key.split(":")[1], meta))
    return sorted(sessions, key=lambda s: s.created_at, reverse=True)


@app.get(
    "/api/sessions/{sid}",
    summary="Одна сессия",
    responses={404: {"description": "Сессии нет или она завершилась больше 5 минут назад"}},
)
async def get_session(sid: str) -> SessionInfo:
    """Показать сведения об одной сессии.

    :param sid: идентификатор сессии.
    """
    meta = await _redis().hgetall(meta_key(sid))
    if not meta:
        raise HTTPException(status_code=404, detail="сессия не найдена")
    return _session_info(sid, meta)


class Session:
    """Одно WebSocket-подключение, привязанное к сессии терминала."""

    def __init__(self, ws: WebSocket, redis: aioredis.Redis, sid: str, last_id: str) -> None:
        """Связать WebSocket с сессией.

        :param ws: принятый WebSocket.
        :param redis: клиент Redis.
        :param sid: идентификатор сессии.
        :param last_id: с какой позиции потока вывода начинать (``0-0`` — с начала).
        """
        self.ws = ws
        self.redis = redis
        self.sid = sid
        self.last_id = last_id

    async def send(self, message: ServerMessage) -> None:
        """Отправить сообщение браузеру.

        :param message: одна из моделей сервер → браузер.
        """
        await self.ws.send_text(message.model_dump_json())

    async def heartbeat(self) -> None:
        """Продлевать ключ-пульс, пока браузер подключён."""
        ttl = settings.heartbeat_ttl
        while True:
            await self.redis.set(alive_key(self.sid), "1", ex=ttl)
            await asyncio.sleep(max(1, ttl // 3))

    async def watch_worker(self) -> None:
        """Закрыть сессию, если worker перестал продлевать её аренду (упал или перезапущен)."""
        while True:
            await asyncio.sleep(OWNER_RENEW)
            status = await self.redis.hget(meta_key(self.sid), "status")
            if status == SessionStatus.RUNNING and not await self.redis.exists(owner_key(self.sid)):
                log.warning("session %s: worker пропал", self.sid)
                await self.redis.hset(meta_key(self.sid), mapping={
                    "status": SessionStatus.LOST, "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                await self.redis.expire(meta_key(self.sid), 300)
                await self.redis.delete(alive_key(self.sid))
                await self.send(ErrorMessage(message="worker с этим psql перезапустился — сессия потеряна"))
                await self.send(ExitMessage(code=-1))
                return

    async def browser_to_redis(self) -> None:
        """Проверить сообщения браузера и переложить их в поток ввода сессии."""
        stream = input_stream(self.sid)
        while True:
            try:
                msg = client_message.validate_json(await self.ws.receive_text())
            except ValidationError as exc:
                await self.send(ErrorMessage(message=f"непонятное сообщение: {exc.errors()[0]['msg']}"))
                continue
            match msg:
                case InputMessage(data=data):
                    await self.redis.xadd(stream, {"kind": Kind.INPUT, "data": data})
                case ResizeMessage(cols=cols, rows=rows):
                    await self.redis.xadd(stream, {"kind": Kind.RESIZE, "cols": cols, "rows": rows})
                case CloseMessage():
                    await self.redis.xadd(stream, {"kind": Kind.CLOSE})

    async def redis_to_browser(self) -> None:
        """Читать поток вывода сессии и отправлять его в браузер, пока psql не завершится."""
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
    """Завести новую сессию и отправить worker'ам заявку на неё.

    :param redis: клиент Redis.
    :param cols: ширина терминала из параметров подключения.
    :param rows: высота терминала из параметров подключения.
    :return: идентификатор новой сессии.
    """
    sid = uuid.uuid4().hex
    # пульс и сведения ставим до заявки, чтобы worker не счёл сессию брошенной
    await redis.set(alive_key(sid), "1", ex=settings.heartbeat_ttl)
    await redis.hset(meta_key(sid), mapping={
        "status": SessionStatus.PENDING, "connections": 0,
        "cols": cols, "rows": rows, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    await redis.expire(meta_key(sid), META_TTL)
    await redis.xadd(CONTROL_STREAM, {"kind": Kind.OPEN, "sid": sid, "cols": cols, "rows": rows})
    return sid


@app.websocket("/ws")
async def terminal_ws(ws: WebSocket) -> None:
    """Подключить браузер к новой или работающей сессии терминала.

    :param ws: WebSocket от браузера.
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
    # Если браузер просто отключился, psql живёт ещё heartbeat_ttl секунд —
    # за это время можно переподключиться с тем же session id.
