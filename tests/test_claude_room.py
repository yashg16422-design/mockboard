"""The local Claude room (`python -m mockboard.claude_room`), with a scripted stand-in for Claude.

No Claude call and no Gnani call: the stand-in drives the same tool path Claude uses
(`runner.make_call`), and speech is fake. A real local server is used throughout.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.request

import pytest

from mockboard.claude_room.app import ClaudeRoomBridge
from mockboard.claude_room.runner import RUN_TOOLS, agent_options, build_server, make_call
from mockboard.mcp.service import BoardService, default_settings

websockets = pytest.importorskip("websockets")
sdk = pytest.importorskip("claude_agent_sdk")

FORM = {"home_state": "Uttar Pradesh", "district": "Lucknow", "degree_subject": "Physics",
        "hobbies": ["Chess"], "interview_language": "english"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def note(text: str):
    return sdk.AssistantMessage(content=[sdk.TextBlock(text=text)], model="stand-in")


async def scripted_claude(prompt, options, call):
    """What Claude does first: start the voice sitting from the form in the prompt, prepare."""
    form = json.loads(prompt.split("form: ", 1)[1])
    start, err = await call("start_voice_interview", {"form": form})
    assert not err, start
    yield note("I read the form: physics, a teaching year, chess. I will test the motivation.")
    await call("prepare_board", {"session_id": start["session_id"], "plan": [
        {"member": "economist", "angle": "Explain one core idea of physics to a farmer",
         "why": "the degree", "weak_spot": "a metaphor instead of the idea"}]})


@pytest.fixture
async def server():
    service = BoardService()
    bridge = ClaudeRoomBridge(service, port=free_port(), fake_speech=True, agent=scripted_claude,
                              settings=default_settings())
    service.voice = bridge
    await bridge.start()
    yield bridge
    await service.aclose()


async def control(bridge, origin: str | None = None):
    origin = origin or f"http://127.0.0.1:{bridge.port}"
    return await websockets.connect(f"ws://127.0.0.1:{bridge.port}/start", origin=origin)


async def read_until(ws, kind: str, timeout: float = 5.0) -> list[dict]:
    seen = []
    async with asyncio.timeout(timeout):
        while True:
            msg = json.loads(await ws.recv())
            seen.append(msg)
            if msg["type"] == kind:
                return seen


async def test_the_start_page_is_served_in_the_web_apps_look(server):
    body = await asyncio.to_thread(
        lambda: urllib.request.urlopen(f"http://127.0.0.1:{server.port}/").read().decode())
    assert "Start with Claude" in body and "window.Presence" in body and "@font-face" in body
    assert "__PORT__" not in body and "/*__TOKENS__*/" not in body


async def test_starting_takes_the_page_to_the_voice_room_and_shows_claudes_notes(server):
    ws = await control(server)
    await ws.send(json.dumps({"type": "start", "mode": "upsc", "form": FORM}))
    seen = await read_until(ws, "room")
    assert seen[0] == {"type": "note", "text": "Claude is reading your form…"}
    url = seen[-1]["url"]
    assert url.startswith("/room?s=mcp-") and "&k=" in url
    page = await websockets.connect(f"ws://127.0.0.1:{server.port}/ws?{url.split('?', 1)[1]}")
    events = []
    async with asyncio.timeout(5):
        while not any(e.get("type") == "agent" for e in events) or sum(
                e.get("type") == "prep" for e in events) < 2:
            events.append(json.loads(await page.recv()))
    agent = next(e for e in events if e["type"] == "agent")
    assert agent["text"].startswith("I read the form")
    await server.run.task
    assert server.run.status == "done"
    await page.close()
    await ws.close()


async def test_only_mock_boards_own_page_may_start_a_run(server):
    ws = await control(server, origin="http://evil.example")
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await ws.recv()
    assert closed.value.rcvd.code == 4403
    assert server.run is None


async def test_a_bad_form_is_refused_before_claude_starts(server):
    ws = await control(server)
    await ws.send(json.dumps({"type": "start", "mode": "upsc",
                              "form": {"home_state": "Kerala"}}))
    msg = json.loads(await ws.recv())
    assert msg["type"] == "error" and server.run is None
    await ws.close()


async def test_one_run_at_a_time(server):
    gate = asyncio.Event()

    async def slow(prompt, options, call):
        await gate.wait()
        yield note("done")

    server.agent = slow
    ws = await control(server)
    await ws.send(json.dumps({"type": "start", "mode": "upsc", "form": FORM}))
    await read_until(ws, "note")
    await ws.send(json.dumps({"type": "start", "mode": "upsc", "form": FORM}))
    msg = json.loads(await ws.recv())
    assert msg["type"] == "error" and "already running" in msg["text"]
    gate.set()
    await server.run.task
    await ws.close()


async def test_a_study_topic_starts_a_partner_run(server):
    async def partner(prompt, options, call):
        payload = json.loads(prompt.split("language: ", 1)[1])
        start, err = await call("start_voice_partner", payload)
        assert not err, start
        yield note("Mitra will start with the basics.")

    server.agent = partner
    ws = await control(server)
    await ws.send(json.dumps({"type": "start", "mode": "partner", "topic": "Federalism in India",
                              "language": "english"}))
    seen = await read_until(ws, "room")
    assert seen[-1]["url"].startswith("/room?s=")
    await server.run.task
    await ws.close()


def test_claude_gets_only_mock_boards_tools_and_none_of_its_own():
    service = BoardService()

    async def nothing(*a):
        return None

    options = agent_options(build_server(make_call(service, nothing)))
    assert options.tools == [] and options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.allowed_tools == [f"mcp__mockboard__{n}" for n in RUN_TOOLS]
    assert "voice_room_ticket" not in RUN_TOOLS and "start_interview" not in RUN_TOOLS


async def test_a_tool_outside_the_list_is_refused():
    service = BoardService()

    async def nothing(*a):
        return None

    result, is_error = await make_call(service, nothing)("likely_questions", {"form": FORM})
    assert is_error and "not available" in result["error"]
