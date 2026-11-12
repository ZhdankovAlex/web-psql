"""gateway без Docker: HTTP API и WebSocket на Redis в памяти (fakeredis)."""

import fakeredis
import pytest
from fastapi.testclient import TestClient

from webpsql import gateway
from webpsql.streams import CONTROL_STREAM, Kind, SessionStatus, alive_key, meta_key, output_stream


@pytest.fixture
def client(monkeypatch):
    """TestClient, у которого вместо настоящего Redis — fakeredis.

    :return: клиент и подменный Redis.
    """
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(gateway.aioredis, "from_url", lambda *a, **kw: fake)
    with TestClient(gateway.app) as c:
        yield c, fakeredis.FakeRedis(server=server, decode_responses=True)


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").json() == {"redis": True}


def test_pages_are_served(client):
    c, _ = client
    assert "Терминал" in c.get("/").text
    assert "Сессии" in c.get("/sessions").text


def test_sessions_empty(client):
    c, _ = client
    assert c.get("/api/sessions").json() == []


def test_session_404(client):
    c, _ = client
    assert c.get("/api/sessions/nope").status_code == 404


def test_sessions_list_newest_first(client):
    c, r = client
    r.hset(meta_key("old"), mapping={"status": "exited", "created_at": "2026-11-12T10:00:00+00:00", "pid": ""})
    r.hset(meta_key("new"), mapping={"status": "running", "created_at": "2026-11-12T11:00:00+00:00", "pid": "7"})
    sessions = c.get("/api/sessions").json()
    assert [s["id"] for s in sessions] == ["new", "old"]
    assert sessions[0]["pid"] == 7
    assert sessions[1]["pid"] is None  # пустая строка из Redis — это «нет значения»


def test_new_session_files_a_request(client):
    c, r = client
    with c.websocket_connect("/ws?cols=100&rows=30") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "session"
        assert hello["resumed"] is False
        sid = hello["id"]

        (_, request), = r.xrange(CONTROL_STREAM)
        assert request == {"kind": Kind.OPEN, "sid": sid, "cols": "100", "rows": "30"}
        assert r.exists(alive_key(sid))
        assert r.hget(meta_key(sid), "status") == SessionStatus.PENDING
        ws.send_json({"type": "close"})


def test_bad_message_gets_error(client):
    c, _ = client
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "bogus"})
        reply = ws.receive_json()
        assert reply["type"] == "error"


def test_resume_replays_missed_output(client):
    """Переподключение к живой сессии отдаёт только вывод после last."""
    c, r = client
    sid = "a" * 32
    r.set(alive_key(sid), "1")
    r.hset(meta_key(sid), mapping={"status": "running", "created_at": "2026-11-12T10:00:00+00:00"})
    first = r.xadd(output_stream(sid), {"kind": "output", "data": "уже видели"})
    r.xadd(output_stream(sid), {"kind": "output", "data": "пропущено"})
    r.xadd(output_stream(sid), {"kind": "exit", "code": "0"})

    with c.websocket_connect(f"/ws?session={sid}&last={first}") as ws:
        assert ws.receive_json() == {"type": "session", "id": sid, "resumed": True}
        out = ws.receive_json()
        assert out["type"] == "output"
        assert out["data"] == "пропущено"
        assert ws.receive_json() == {"type": "exit", "code": 0}


def test_unknown_session_starts_new_one(client):
    c, _ = client
    with c.websocket_connect("/ws?session=" + "b" * 32) as ws:
        hello = ws.receive_json()
        assert hello["resumed"] is False
        assert hello["id"] != "b" * 32
        ws.send_json({"type": "close"})
