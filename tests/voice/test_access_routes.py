"""The access limits through the real routes: a fake WebRTC handler, no browser, no speech."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import ClassVar

import pytest

pytest.importorskip("pipecat")

from fastapi.testclient import TestClient

from mockboard.access import COOKIE, AccessConfig, Limits, sign_token
from mockboard.voice import server

SECRET = "route-test-secret-not-a-real-key-000"
CODE = "route-reviewer-code"
FORM = {"home_state": "Goa", "degree_subject": "History"}


class FakePC:
    def __init__(self, state: str):
        self.connectionState = state

    def on(self, _event, _callback) -> None:
        pass

    def remove_listener(self, _event, _callback) -> None:
        pass


class FakeConnection:
    count = 0

    def __init__(self, state: str):
        FakeConnection.count += 1
        self.pc_id = f"pc-{FakeConnection.count}"
        self.pc = FakePC(state)
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeHandler:
    state = "connected"
    connections: ClassVar[list[FakeConnection]] = []

    async def handle_web_request(self, request, webrtc_connection_callback):
        connection = FakeConnection(self.state)
        FakeHandler.connections.append(connection)
        await webrtc_connection_callback(connection)
        return {"sdp": "answer", "type": "answer", "pc_id": connection.pc_id}

    async def close(self) -> None:
        pass


@pytest.fixture
def ran(monkeypatch, tmp_path):
    """Limits on, a fake handler, and the sessions `run_session` was given."""
    for name, value in {
        "ACCESS_ENABLED": "true", "ACCESS_SECRET": SECRET, "ACCESS_REVIEWER_CODES": CODE,
        "ACCESS_FREE_SESSIONS": "2", "ACCESS_DB": str(tmp_path / "access.sqlite"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(server, "SmallWebRTCRequestHandler", FakeHandler)
    monkeypatch.setattr(FakeHandler, "state", "connected")
    monkeypatch.setattr(FakeHandler, "connections", [])
    sessions: list[object] = []

    async def fake_run(connection, cfg, form):
        sessions.append((connection, form))

    monkeypatch.setattr(server, "run_session", fake_run)
    return sessions


def app_for(settings, timeout_s: float = 0.05):
    cfg = server.parse_args(["--mode", "upsc", "--fake-llm", "--fake-speech"])
    cfg.settings = settings.model_copy(update={"voice_connect_timeout_s": timeout_s})
    return server.create_app(cfg)


def offer(client: TestClient, page: str = "page", **data):
    body = {"form": FORM, "page_id": page, **data}
    return client.post("/api/offer", json={"sdp": "x", "type": "offer", "request_data": body})


def settle(client: TestClient, done) -> None:
    deadline = time.monotonic() + 3
    while not done() and time.monotonic() < deadline:
        client.get("/api/config")  # lets the app's event loop run
        time.sleep(0.01)
    assert done()


def rows(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def db_path() -> Path:
    return AccessConfig.from_env().db_path


# --- Counting (CTO_NOTES_13 M2) ---------------------------------------------------------------


def test_a_connection_that_connects_writes_one_row_and_runs(ran, settings):
    with TestClient(app_for(settings)) as client:
        assert offer(client).status_code == 200
        settle(client, lambda: ran)
    assert rows(db_path()) == 1 and len(ran) == 1
    assert ran[0][1].home_state == "Goa"


def test_a_connection_that_never_connects_writes_nothing(ran, settings):
    FakeHandler.state = "new"
    with TestClient(app_for(settings)) as client:
        assert offer(client).status_code == 200
        gate = client.app.state.gate
        settle(client, lambda: gate.abandoned)
    assert rows(db_path()) == 0 and not ran


def test_a_commit_refused_at_connect_never_runs_a_session(ran, settings, monkeypatch):
    calls: list[int] = []

    def refuse(_self, _identity, _now):
        calls.append(1)
        return False

    monkeypatch.setattr(Limits, "_commit", refuse)
    with TestClient(app_for(settings)) as client:
        assert offer(client).status_code == 200
        settle(client, lambda: calls and FakeHandler.connections[0].disconnected)
    assert not ran


def test_a_full_quota_refuses_the_offer_after_real_sessions(ran, settings):
    with TestClient(app_for(settings)) as client:
        for n in (1, 2):
            assert offer(client, f"page-{n}").status_code == 200
            settle(client, lambda n=n: len(ran) == n)
        refused = offer(client, "page-3")
    assert refused.status_code == 429 and refused.json()["reason"] == "free_used"


# --- Reviewer codes (S3, S4) ------------------------------------------------------------------


def test_a_wrong_code_at_the_offer_spends_nothing(ran, settings):
    with TestClient(app_for(settings)) as client:
        refused = offer(client, access_code="nope")
        assert refused.status_code == 403
        assert refused.json() == {"error": "code", "reason": "wrong"}
    assert rows(db_path()) == 0 and not ran and not FakeHandler.connections


def test_a_right_code_at_the_offer_runs_uncounted(ran, settings):
    with TestClient(app_for(settings)) as client:
        assert offer(client, access_code=CODE).status_code == 200
        settle(client, lambda: ran)
    assert rows(db_path()) == 0


def test_the_code_route_grants_reviewer_and_tells_a_lockout(ran, settings):
    with TestClient(app_for(settings)) as client:
        wrong = client.post("/api/access", json={"code": "nope"}).json()
        assert wrong["code"] == "wrong" and wrong["reviewer"] is False
        for _ in range(8):
            last = client.post("/api/access", json={"code": "nope"}).json()
        assert last["code"] == "locked"
        locked = client.post("/api/access", json={"code": CODE}).json()
        assert locked["code"] == "locked" and locked["reviewer"] is False


def test_the_code_route_sets_a_cookie_that_works(ran, settings):
    with TestClient(app_for(settings)) as client:
        done = client.post("/api/access", json={"code": CODE}).json()
        assert done["code"] == "ok" and done["reviewer"] is True
        assert done["answers_per_interview"] is None
        assert client.get("/api/access").json()["reviewer"] is True


def test_the_code_route_survives_a_bad_body(ran, settings):
    with TestClient(app_for(settings)) as client:
        response = client.post("/api/access", content="not json")
        assert response.status_code == 200 and response.json()["code"] == "wrong"


def test_removing_a_code_revokes_its_cookie(ran, settings, monkeypatch):
    monkeypatch.setenv("ACCESS_REVIEWER_CODES", "a-different-code")
    token = sign_token("owner", True, SECRET, code=CODE)
    with TestClient(app_for(settings)) as client:
        client.cookies.set(COOKIE, token)
        assert client.get("/api/access").json()["reviewer"] is False


# --- Offers in flight and cleanup (S6), and `pending_limits` -----------------------------------


def test_too_many_offers_in_flight_are_refused_and_abandoned_ones_clear(ran, settings):
    FakeHandler.state = "new"
    with TestClient(app_for(settings, timeout_s=0.3)) as client:
        for n in range(server.MAX_OFFERS_PER_IP):
            assert offer(client, f"p{n}").status_code == 200
        busy = offer(client, "one-more")
        assert busy.status_code == 429 and busy.json() == {"error": "busy"}
        gate = client.app.state.gate
        settle(client, lambda: len(gate.abandoned) == server.MAX_OFFERS_PER_IP)
        assert offer(client, "after").status_code == 200


def test_pending_entries_go_for_an_abandoned_connection(ran, settings):
    FakeHandler.state = "new"
    with TestClient(app_for(settings)) as client:
        assert offer(client).status_code == 200
        gate = client.app.state.gate
        settle(client, lambda: gate.abandoned)
        assert not any(client.app.state.pending.values())


def test_pending_limits_holds_the_cap_during_a_session(ran, settings, monkeypatch):
    seen: list[int | None] = []
    app = app_for(settings)
    pending = app.state.pending["limits"]

    async def peek(connection, cfg, form):
        seen.append(pending[connection.pc_id])

    monkeypatch.setattr(server, "run_session", peek)
    with TestClient(app) as client:
        assert offer(client).status_code == 200
        settle(client, lambda: seen)
        assert offer(client, "reviewer", access_code=CODE).status_code == 200
        settle(client, lambda: len(seen) == 2)
        settle(client, lambda: not pending)
    assert seen == [10, None]


# --- Static files and startup ------------------------------------------------------------------


@pytest.mark.parametrize(
    "asset",
    ["lab.html", "LAB.HTML", "Lab.html", "lab.html%00", "..%2fserver.py", "%2e%2e/server.py",
     ".hidden", "index.html", "sub/app.js"],
)
def test_only_the_four_page_files_are_served(ran, settings, asset: str):
    with TestClient(app_for(settings)) as client:
        assert client.get(f"/static/{asset}").status_code == 404


@pytest.mark.parametrize("asset", ["app.js", "presence.js", "site.css", "tokens.css"])
def test_the_page_files_are_served(ran, settings, asset: str):
    with TestClient(app_for(settings)) as client:
        assert client.get(f"/static/{asset}").status_code == 200


def test_the_api_docs_are_closed(ran, settings):
    with TestClient(app_for(settings)) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404


def _cfg(host: str, *flags: str):
    return server.parse_args(["--mode", "upsc", "--host", host, *flags])


@pytest.mark.parametrize("flags", [(), ("--fake-llm",), ("--fake-speech",)])
def test_real_services_on_a_public_host_with_limits_off_refuse_to_start(flags):
    with pytest.raises(SystemExit):
        server._refuse_exposure(_cfg("0.0.0.0", *flags), AccessConfig(enabled=False))


def test_a_safe_host_or_limits_on_or_all_fakes_may_start():
    off = AccessConfig(enabled=False)
    server._refuse_exposure(_cfg("localhost"), off)
    server._refuse_exposure(_cfg("127.0.0.1"), off)
    server._refuse_exposure(_cfg("0.0.0.0", "--fake-llm", "--fake-speech"), off)
    server._refuse_exposure(_cfg("0.0.0.0"), AccessConfig(enabled=True))


def test_uvicorn_gets_no_access_log_when_limits_are_on(ran, monkeypatch):
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    server.main(["--mode", "upsc", "--fake-llm", "--fake-speech"])
    assert captured["access_log"] is False


def test_work_experience_reaches_the_form():
    form = server.parse_form({**FORM, "work_experience": "Taught for a year"}, allow_default=False)
    assert form.work_experience == "Taught for a year"
