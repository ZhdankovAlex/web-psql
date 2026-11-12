"""WebSocket message checks: what gets through and what is rejected."""

import json

import pytest
from pydantic import ValidationError

from webpsql.protocol import (
    CloseMessage,
    ExitMessage,
    InputMessage,
    OutputMessage,
    ResizeMessage,
    SessionInfo,
    client_message,
)


def parse(payload: dict):
    """Parse a message the same way the gateway does.

    :param payload: message from the browser.
    :return: message model.
    """
    return client_message.validate_json(json.dumps(payload))


def test_input_is_parsed():
    msg = parse({"type": "input", "data": "select 1;\r"})
    assert isinstance(msg, InputMessage)
    assert msg.data == "select 1;\r"


def test_resize_is_parsed():
    msg = parse({"type": "resize", "cols": 120, "rows": 40})
    assert isinstance(msg, ResizeMessage)
    assert (msg.cols, msg.rows) == (120, 40)


def test_close_is_parsed():
    assert isinstance(parse({"type": "close"}), CloseMessage)


@pytest.mark.parametrize("payload", [
    {"type": "bogus"},
    {"data": "no type"},
    {"type": "input"},                              # no data
    {"type": "input", "data": 42},                  # data is not a string
    {"type": "resize", "cols": 99999, "rows": 10},  # window larger than allowed
    {"type": "resize", "cols": 0, "rows": 10},
    {"type": "resize", "cols": "wide", "rows": 10},
    {"type": "input", "data": "x" * (64 * 1024 + 1)},
])
def test_bad_messages_are_rejected(payload):
    with pytest.raises(ValidationError):
        parse(payload)


def test_not_json_is_rejected():
    with pytest.raises(ValidationError):
        client_message.validate_json("this is not JSON")


def test_server_messages_carry_type():
    assert json.loads(OutputMessage(data="hi", id="1-0").model_dump_json()) == {
        "type": "output", "data": "hi", "id": "1-0",
    }
    assert json.loads(ExitMessage(code=0).model_dump_json())["type"] == "exit"


def test_session_info_from_redis_strings():
    """Redis returns everything as strings — the model must convert the types itself."""
    info = SessionInfo(
        id="abc", status="running", pid="12", connections="1", cols="80", rows="24",
        created_at="2026-11-12T19:00:00+00:00", sandbox_ms="47",
    )
    assert info.pid == 12
    assert info.sandbox_ms == 47
    assert info.connections == 1
