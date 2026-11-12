"""Протокол WebSocket ``/ws`` между браузером и gateway, а также модели HTTP-ответов.

Каждое сообщение — JSON-объект с полем ``type`` (:class:`MessageType`).
Входящие сообщения разбираются в одну из моделей :data:`ClientMessage`
по значению ``type`` (discriminated union), поэтому неизвестный тип или
неверные поля отсекаются валидацией, а не ``if``-ами в обработчике.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from webpsql.streams import SessionStatus


class MessageType(StrEnum):
    """Тип WebSocket-сообщения."""

    # браузер -> сервер
    INPUT = "input"
    RESIZE = "resize"
    CLOSE = "close"
    # сервер -> браузер
    SESSION = "session"
    OUTPUT = "output"
    ERROR = "error"
    EXIT = "exit"


# --- браузер -> сервер -------------------------------------------------------

class InputMessage(BaseModel):
    """Нажатия клавиш или вставленный текст."""

    type: Literal[MessageType.INPUT]
    data: str = Field(max_length=64 * 1024, description="Символы так, как их отдал xterm.js")


class ResizeMessage(BaseModel):
    """Новый размер окна терминала."""

    type: Literal[MessageType.RESIZE]
    cols: int = Field(ge=2, le=1000, description="Ширина в символах")
    rows: int = Field(ge=2, le=1000, description="Высота в строках")


class CloseMessage(BaseModel):
    """Завершить psql."""

    type: Literal[MessageType.CLOSE]


ClientMessage = Annotated[
    Union[InputMessage, ResizeMessage, CloseMessage],
    Field(discriminator="type"),
]
client_message = TypeAdapter(ClientMessage)
"""Разбор входящего JSON: ``client_message.validate_json(text)``."""


# --- сервер -> браузер -------------------------------------------------------

class SessionMessage(BaseModel):
    """Первое сообщение после подключения: к какой сессии привязан WebSocket."""

    type: Literal[MessageType.SESSION] = MessageType.SESSION
    id: str = Field(description="Идентификатор сессии")
    resumed: bool = Field(description="True — подключились к уже работающему psql")


class OutputMessage(BaseModel):
    """Кусок вывода терминала."""

    type: Literal[MessageType.OUTPUT] = MessageType.OUTPUT
    data: str
    id: str = Field(description="Позиция в потоке Redis; с неё продолжается чтение после переподключения")


class ErrorMessage(BaseModel):
    """Ошибка, которую стоит показать пользователю."""

    type: Literal[MessageType.ERROR] = MessageType.ERROR
    message: str


class ExitMessage(BaseModel):
    """psql завершился, сессия закрыта."""

    type: Literal[MessageType.EXIT] = MessageType.EXIT
    code: int = Field(
        description="Код выхода psql. Отрицательный — psql остановлен сигналом "
                    "(-1 = SIGHUP при закрытии сессии) или не запускался",
    )


ServerMessage = Union[SessionMessage, OutputMessage, ErrorMessage, ExitMessage]


# --- HTTP ---------------------------------------------------------------------

class SessionInfo(BaseModel):
    """Сведения о сессии терминала."""

    id: str = Field(description="Идентификатор сессии")
    status: SessionStatus = Field(description="Состояние сессии")
    worker: str | None = Field(None, description="Worker, на котором запущен psql")
    pid: int | None = Field(None, description="PID процесса psql внутри контейнера worker'а")
    database: str | None = Field(None, description="Копия учебной базы, которую получила сессия")
    sandbox_ms: int | None = Field(None, description="Сколько миллисекунд создавалась копия базы")
    connections: int = Field(0, description="Сколько вкладок браузера сейчас подключено")
    cols: int | None = Field(None, description="Ширина терминала")
    rows: int | None = Field(None, description="Высота терминала")
    created_at: datetime = Field(description="Когда открыта")
    finished_at: datetime | None = Field(None, description="Когда завершился psql")
    exit_code: int | None = Field(None, description="Код выхода psql")
