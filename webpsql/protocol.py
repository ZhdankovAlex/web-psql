"""Messages of the ``/ws`` WebSocket between the browser and the gateway, plus HTTP response models.

Every message is a JSON object with a ``type`` field (:class:`MessageType`).
The value of ``type`` picks which :data:`ClientMessage` model checks an incoming message.
An unknown type or wrong fields are rejected by that check, not by a chain of ``if``
statements in the handler.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from webpsql.streams import SessionStatus


class MessageType(StrEnum):
    """WebSocket message type."""

    # browser -> server
    INPUT = "input"
    RESIZE = "resize"
    CLOSE = "close"
    # server -> browser
    SESSION = "session"
    OUTPUT = "output"
    ERROR = "error"
    EXIT = "exit"


# --- browser -> server --------------------------------------------------------

class InputMessage(BaseModel):
    """Key presses or pasted text."""

    type: Literal[MessageType.INPUT]
    data: str = Field(max_length=64 * 1024, description="Characters exactly as xterm.js produced them")


class ResizeMessage(BaseModel):
    """New terminal window size."""

    type: Literal[MessageType.RESIZE]
    cols: int = Field(ge=2, le=1000, description="Width in characters")
    rows: int = Field(ge=2, le=1000, description="Height in lines")


class CloseMessage(BaseModel):
    """Stop psql."""

    type: Literal[MessageType.CLOSE]


ClientMessage = Annotated[
    InputMessage | ResizeMessage | CloseMessage,
    Field(discriminator="type"),
]
client_message = TypeAdapter(ClientMessage)
"""Parses incoming JSON: ``client_message.validate_json(text)``."""


# --- server -> browser --------------------------------------------------------

class SessionMessage(BaseModel):
    """First message after connecting: which session the WebSocket is attached to."""

    type: Literal[MessageType.SESSION] = MessageType.SESSION
    id: str = Field(description="Session id")
    resumed: bool = Field(description="True if we reconnected to an already running psql")


class OutputMessage(BaseModel):
    """A piece of terminal output."""

    type: Literal[MessageType.OUTPUT] = MessageType.OUTPUT
    data: str
    id: str = Field(description="Position in the Redis stream; reading resumes from here after a reconnect")


class ErrorMessage(BaseModel):
    """An error worth showing to the user."""

    type: Literal[MessageType.ERROR] = MessageType.ERROR
    message: str


class ExitMessage(BaseModel):
    """psql has exited, the session is over."""

    type: Literal[MessageType.EXIT] = MessageType.EXIT
    code: int = Field(
        description="psql exit code. Negative means psql was stopped by a signal "
                    "(-1 = SIGHUP when the session is closed) or never started",
    )


ServerMessage = SessionMessage | OutputMessage | ErrorMessage | ExitMessage


# --- HTTP ---------------------------------------------------------------------

class SessionInfo(BaseModel):
    """Terminal session details."""

    id: str = Field(description="Session id")
    status: SessionStatus = Field(description="Session state")
    worker: str | None = Field(None, description="Worker running psql")
    pid: int | None = Field(None, description="PID of the psql process inside the worker container")
    database: str | None = Field(None, description="The copy of the demo database this session got")
    sandbox_ms: int | None = Field(None, description="How many milliseconds it took to create the copy")
    connections: int = Field(0, description="How many browser tabs are connected right now")
    cols: int | None = Field(None, description="Terminal width")
    rows: int | None = Field(None, description="Terminal height")
    created_at: datetime = Field(description="When it was opened")
    finished_at: datetime | None = Field(None, description="When psql exited")
    exit_code: int | None = Field(None, description="psql exit code")
