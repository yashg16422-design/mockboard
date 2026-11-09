"""The Gnani voice room for the MCP channel (Option 2, owner Nov 9), with fake speech: free.

A real local bridge on 127.0.0.1 and a WebSocket client that plays the page: the preparation graph
arrives, the board's lines are spoken (audio headers and PCM, acknowledged as played), a typed
answer comes back through `wait_for_answer`, and each decision is drawn. Gnani itself is not called.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from mockboard.mcp.safety import RefusedInput, check_plan
from mockboard.mcp.service import BoardService, default_settings
from mockboard.mcp.stdio import StdioServer
from mockboard.mcp.tools import APP_MIME, TOOLS, VIEW_URI, call_tool
from mockboard.mcp.voice_bridge import FakeSpeech, VoiceBridge, page_html, tts_chunks

websockets = pytest.importorskip("websockets")

FORM = {"home_state": "Uttar Pradesh", "district": "Lucknow", "degree_subject": "Physics",
        "hobbies": ["Chess"], "work_experience": "One year as a school science teacher",
        "interview_language": "english"}
PLAN = [
    {"member": "economist", "angle": "Explain one core idea of physics to a farmer",
     "why": "tests whether the degree is understood, not recited",
     "weak_spot": "may reach for a metaphor instead of the idea"},
    {"member": "ips_officer", "angle": "Flood relief that falls short: who gets it first",
     "why": "the case study", "weak_spot": "pressure from a local politician"},
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def service():
    svc = BoardService()
    port = free_port()
    svc.voice_factory = lambda: VoiceBridge(default_settings(), lambda s: FakeSpeech(), port)
    yield svc
    await svc.aclose()


class Page:
    """Plays the voice room page: records every event, acknowledges every clip."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.events: list[dict] = []
        self.audio: list[bytes] = []
        self._head = None
        self.task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for data in self.ws:
            if isinstance(data, bytes):
                self.audio.append(data)
                await self.ws.send(json.dumps({"type": "played", "seq": self._head["seq"]}))
                continue
            event = json.loads(data)
            self.events.append(event)
            if event.get("type") == "audio":
                self._head = event

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind]

    async def until(self, check, timeout: float = 5.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while not check():
            assert asyncio.get_running_loop().time() < end, self.events[-5:]
            await asyncio.sleep(0.02)


async def open_page(ws_url: str) -> Page:
    return Page(await websockets.connect(ws_url))


def test_the_voice_tools_are_listed_and_the_start_tools_carry_the_panel():
    by_name = {t["name"]: t for t in TOOLS}
    for name in ("start_voice_interview", "start_voice_partner"):
        assert by_name[name]["_meta"]["ui"]["resourceUri"] == VIEW_URI
    assert by_name["voice_room_ticket"]["_meta"]["ui"]["visibility"] == ["app"]
    for name in ("prepare_board", "say", "wait_for_answer"):
        assert name in by_name


def test_the_page_carries_the_web_apps_look_and_a_port():
    html = page_html(8799)
    assert "8799" in html and "__PORT__" not in html
    assert "window.Presence" in html and "@font-face" in html and "--mint" in html
    assert "/*__TOKENS__*/" not in html and "//__PRESENCE__" not in html


async def test_the_panel_resource_is_served_with_its_permissions(service):
    server = StdioServer(service)
    out = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                               "params": {"uri": VIEW_URI}})
    content = out["result"]["contents"][0]
    assert content["mimeType"] == APP_MIME and "Mock Board" in content["text"]
    meta = content["_meta"]["ui"]
    assert "microphone" in meta["permissions"]
    assert f"ws://127.0.0.1:{service.voice.port}" in meta["csp"]["connectDomains"]


async def test_a_spoken_sitting_end_to_end_with_fake_speech(service):
    start, err = await call_tool(service, "start_voice_interview", {"form": FORM})
    assert not err, start
    sid, voice = start["session_id"], start["voice"]
    assert voice["voice_url"].startswith("http://127.0.0.1:") and "k=" in voice["voice_url"]
    page = await open_page(voice["ws_url"])
    await page.until(lambda: page.of("prep"))
    prep = page.of("prep")[0]
    assert [m["id"] for m in prep["members"]][:2] == ["chairman", "economist"]
    assert all(m["voice"] for m in prep["members"])
    assert {f["key"] for f in prep["facts"]} >= {"degree", "district", "work"}

    planned, err = await call_tool(service, "prepare_board", {"session_id": sid, "plan": PLAN})
    assert not err and planned["drawn"] == 2
    await page.until(lambda: len(page.of("prep")) == 2)
    seg = next(s for s in page.of("prep")[1]["segments"] if s["member"] == "economist")
    assert seg["lines"][0]["weak_spot"].startswith("may reach")
    await page.ws.send(json.dumps({"type": "ready"}))

    opening = [{"member": x["member"], "text": x.get("text") or x["draft"]} for x in start["say"]]
    said, err = await call_tool(service, "say", {"session_id": sid, "lines": opening})
    assert not err and said["spoken"] is True and said["lines"] == len(opening)
    assert [c["text"] for c in page.of("caption")] == [x["text"] for x in opening]
    assert len(page.audio) == len(page.of("audio")) >= len(opening)
    await page.until(lambda: [e["state"] for e in page.of("state")][-1:] == ["listening"])

    waiting = asyncio.create_task(call_tool(service, "wait_for_answer", {"session_id": sid}))
    await asyncio.sleep(0.05)
    answer = "Physics taught me to break a problem into parts, because that is how systems work."
    await page.ws.send(json.dumps({"type": "typed_answer", "text": answer}))
    got, err = await waiting
    assert not err and got["status"] == "answered" and got["candidate_said"] == answer
    assert page.of("heard")[-1]["text"] == answer

    turn, err = await call_tool(service, "next_turn", {"session_id": sid, "candidate_said": answer})
    assert not err
    await page.until(lambda: page.of("decision"))
    decision = page.of("decision")[-1]
    assert decision["rule"] == turn["board"]["rule"] and decision["turn_id"] == turn["turn_id"]
    assert decision["member_name"] and decision["segment_id"]
    await page.ws.close()


async def test_say_without_an_open_page_says_so_and_wait_returns_waiting(service):
    start, _ = await call_tool(service, "start_voice_interview", {"form": FORM})
    sid = start["session_id"]
    said, err = await call_tool(service, "say", {"session_id": sid, "lines": [
        {"member": "chairman", "text": "Please introduce yourself."}]})
    assert not err and said["spoken"] is False and "open" in said["reason"]
    got, err = await call_tool(service, "wait_for_answer", {"session_id": sid, "timeout_s": 5})
    assert not err and got["status"] == "waiting"


async def test_a_page_with_the_wrong_token_gets_nothing(service):
    start, _ = await call_tool(service, "start_voice_interview", {"form": FORM})
    bad = start["voice"]["ws_url"].rsplit("k=", 1)[0] + "k=wrong"
    ws = await websockets.connect(bad)
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await ws.recv()
    assert closed.value.rcvd.code == 4403


async def test_the_text_tools_still_work_without_a_voice_room(service):
    start, _ = await call_tool(service, "start_interview", {"form": FORM})
    out, err = await call_tool(service, "say", {"session_id": start["session_id"], "lines": [
        {"member": "chairman", "text": "Hello."}]})
    assert err and "no voice room" in out["error"]
    assert service.voice is None  # no port is opened for a text sitting


async def test_a_spoken_study_session_opens_with_mitra(service):
    start, err = await call_tool(service, "start_voice_partner",
                                 {"topic": "Federalism in India", "language": "english"})
    assert not err
    page = await open_page(start["voice"]["ws_url"])
    await page.until(lambda: page.of("prep"))
    prep = page.of("prep")[0]
    assert prep["mode"] == "study_partner" and prep["facts"][0]["value"] == "Federalism in India"
    await page.ws.close()


def test_the_plan_check_keeps_clean_lines_and_drops_the_rest():
    members = {"economist", "ips_officer"}
    kept, dropped = check_plan([
        *PLAN,
        {"member": "economist", "angle": "Ask about the 2023 budget", "why": "x"},  # a figure
        {"member": "nobody", "angle": "A line for no member", "why": "x"},
        {"member": "economist", "angle": "See https://example.com", "why": "x"},  # a link
    ], members)
    assert [x["member"] for x in kept] == ["economist", "ips_officer"] and dropped == 3
    with pytest.raises(RefusedInput):
        check_plan([{"member": "economist", "angle": "short"}], members)
    many = [{"member": "economist", "angle": f"Line number {w} of the plan", "why": "x"}
            for w in ("one", "two", "three", "four")]
    kept, dropped = check_plan(many, members)
    assert len(kept) == 3 and dropped == 1


def test_long_lines_are_cut_into_requests_timbre_accepts():
    text = ("Ek baat saaf kar doon. " * 30).strip()
    chunks = tts_chunks(text)
    assert all(len(c) <= 380 for c in chunks) and " ".join(chunks) == text
