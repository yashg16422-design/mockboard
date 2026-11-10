"""The owner's second test of the Claude room (Nov 10): "wait only a second or two", "nudge me in
the middle of my answer", and "even if I interrupt him while it is speaking he should keep that in
mind". Free: scripted speech, a real local WebSocket playing the page, no Gnani, no model.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from mockboard.mcp.service import BoardService, default_settings
from mockboard.mcp.tools import call_tool
from mockboard.mcp.voice_bridge import VoiceBridge

websockets = pytest.importorskip("websockets")

FORM = {"home_state": "Uttar Pradesh", "district": "Lucknow", "degree_subject": "Physics",
        "hobbies": ["Chess"], "interview_language": "hinglish"}
LONG = ("Main pehle sabse prabhavit logon se baat karunga, kyunki samasya woh sabse achhi jaante "
        "hain, phir laagat aur laabh tolunga.")


class ScriptListener:
    def __init__(self) -> None:
        self.finals: list[str] = []
        self.on_final = None

    def push(self, text: str) -> None:
        self.finals.append(text)
        if self.on_final:
            self.on_final(text)

    def heard(self) -> str:
        return " ".join(self.finals)

    async def feed(self, pcm: bytes) -> None:
        return None

    async def finish(self) -> str:
        return self.heard()

    async def close(self) -> None:
        return None


class ScriptSpeech:
    sample_rate = 16000

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.listeners: list[ScriptListener] = []

    async def synthesize(self, text, voice, language):
        self.spoken.append(text)
        return b"\x00\x00" * 1600  # a tenth of a second

    async def listener(self, lang_code):
        self.listeners.append(ScriptListener())
        return self.listeners[-1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def rig():
    svc = BoardService()
    speech = ScriptSpeech()
    svc.voice_factory = lambda: VoiceBridge(default_settings(), lambda s: speech, free_port())
    start, err = await call_tool(svc, "start_voice_interview", {"form": FORM})
    assert not err
    page = await websockets.connect(start["voice"]["ws_url"])
    seen: list[dict] = []
    hold = asyncio.Event()  # cleared: the page stops acknowledging (audio still "playing")
    hold.set()

    async def play():
        head = None
        async for data in page:
            if isinstance(data, bytes):
                await hold.wait()
                await asyncio.sleep(0.02)
                await page.send(json.dumps({"type": "played", "seq": head["seq"]}))
            else:
                event = json.loads(data)
                seen.append(event)
                if event.get("type") == "audio":
                    head = event

    reader = asyncio.create_task(play())
    await page.send(json.dumps({"type": "ready"}))
    yield svc, start, page, speech, seen, hold
    reader.cancel()
    await page.close()
    await svc.aclose()


async def opening(svc, start):
    lines = [{"member": x["member"], "text": x.get("text") or x["draft"]} for x in start["say"]]
    out, err = await call_tool(svc, "say", {"session_id": start["session_id"], "lines": lines})
    assert not err and out["spoken"]


async def answer_by_voice(page, speech, *parts):
    await page.send(b"\x00\x01" * 800)
    await asyncio.sleep(0.05)
    for part in parts:
        speech.listeners[-1].push(part)
    await page.send(json.dumps({"type": "turn_end"}))


async def test_the_board_decides_the_moment_the_answer_ends(rig):
    svc, start, page, speech, _seen, _ = rig
    sid = start["session_id"]
    await opening(svc, start)
    read = {"analysis": {"stance": "wants to change the system", "reasons": [],
                         "balance": "unclear", "honest_unknown": False, "concedes_error": False,
                         "garbled": False, "candidate_request": "none"},
            "proposal": {"action": "elaborate", "reason": "the journey is missing"}}
    first = asyncio.create_task(call_tool(svc, "wait_for_answer", {
        "session_id": sid, "interim": True, "decide": True}))
    await page.send(b"\x00\x01" * 800)
    await asyncio.sleep(0.05)
    speech.listeners[-1].push("Main Lucknow se hoon aur physics padhi hai.")
    got, err = await first
    assert not err and got["status"] == "speaking"
    then = asyncio.create_task(call_tool(svc, "wait_for_answer", {  # the read goes with it
        "session_id": sid, "interim": True, "decide": True, "read": read}))
    await asyncio.sleep(0.05)
    await page.send(json.dumps({"type": "turn_end"}))
    got, err = await then
    assert not err, got
    assert got["status"] == "answered" and "turn" in got
    assert got["turn"]["board"]["rule"] == "M-ELABORATE"
    assert got["turn"]["board"]["your_proposal"]["accepted"] is True
    assert "say" in got["next_step"]


async def test_a_read_that_no_longer_fits_still_gets_a_decision(rig):
    svc, start, page, speech, _, _ = rig
    sid = start["session_id"]
    await opening(svc, start)
    first = asyncio.create_task(call_tool(svc, "wait_for_answer", {
        "session_id": sid, "interim": True, "decide": True}))
    await page.send(b"\x00\x01" * 800)
    await asyncio.sleep(0.05)
    speech.listeners[-1].push(LONG)
    got, err = await first
    assert not err and got["status"] == "speaking"
    then = asyncio.create_task(call_tool(svc, "wait_for_answer", {
        "session_id": sid, "interim": True, "decide": True,
        "read": {"proposal": {"action": "confront", "quote": "words never said",
                              "reason": "x"}}}))
    await asyncio.sleep(0.05)
    await page.send(json.dumps({"type": "turn_end"}))
    got, err = await then
    assert not err and got["turn"]["board"]["rule"]
    again, err = await call_tool(svc, "next_turn", {"session_id": sid,
                                                    "candidate_said": LONG})
    assert err and "already decided" in again["error"]  # one answer is never counted twice


async def test_a_short_answer_not_read_goes_back_to_the_ai(rig):
    svc, start, page, speech, _, _ = rig
    sid = start["session_id"]
    await opening(svc, start)
    waiting = asyncio.create_task(call_tool(svc, "wait_for_answer", {
        "session_id": sid, "interim": True, "decide": True,
        "read": {"proposal": {"action": "elaborate", "reason": "a read of the last answer"}}}))
    await asyncio.sleep(0.05)
    await answer_by_voice(page, speech, "Can we stop here please?")
    got, err = await waiting
    assert not err and got["status"] == "answered" and "turn" not in got
    _out, err = await call_tool(svc, "next_turn", {"session_id": sid,
                                                  "candidate_said": got["candidate_said"]})
    assert not err


async def test_the_member_reacts_at_once_when_the_answer_ends(rig):
    svc, start, page, speech, seen, _ = rig
    sid = start["session_id"]
    await opening(svc, start)
    await asyncio.sleep(0.1)  # the voices' short reactions are made in the background
    await answer_by_voice(page, speech, LONG)
    got, err = await call_tool(svc, "wait_for_answer", {"session_id": sid})
    assert not err and got["status"] == "answered"
    reaction = [e for e in seen if e.get("type") == "audio" and e.get("reaction")]
    assert reaction and reaction[0]["member"] == "chairman"
    assert {"हम्म।", "जी।"} <= set(speech.spoken)


async def test_a_nudge_is_said_without_ending_the_answer(rig):
    svc, start, page, speech, seen, _ = rig
    sid = start["session_id"]
    await opening(svc, start)
    refused, err = await call_tool(svc, "nudge", {"session_id": sid, "member": "chairman",
                                                  "text": "Aur?"})
    assert err and "not speaking" in refused["error"]
    await page.send(b"\x00\x01" * 800)
    await asyncio.sleep(0.05)
    speech.listeners[-1].push("Sir main ek kahani sunata hoon jo bahut lambi hai")
    long_nudge, err = await call_tool(svc, "nudge", {
        "session_id": sid, "member": "chairman",
        "text": "Kripya seedhe mudde par aaiye, samay kam hai"})
    assert err and "at most 6 words" in long_nudge["error"]
    for _ in range(2):
        out, err = await call_tool(svc, "nudge", {"session_id": sid, "member": "chairman",
                                                  "text": "Point par aaiye.",
                                                  "speak": "पॉइंट पर आइए।"})
        assert not err and out["nudged"]
    third, err = await call_tool(svc, "nudge", {"session_id": sid, "member": "chairman",
                                                "text": "Aur?"})
    assert err and "two nudges" in third["error"]
    assert svc.voice.room(sid).state == "listening"  # the answer goes on
    await asyncio.sleep(0.05)
    assert any(e.get("type") == "audio" and e.get("nudge") for e in seen)
    assert "पॉइंट पर आइए।" in speech.spoken


async def test_speaking_over_the_board_stops_it_and_it_remembers(rig):
    svc, start, page, speech, seen, hold = rig
    sid = start["session_id"]
    await opening(svc, start)
    await answer_by_voice(page, speech, LONG)
    got, _ = await call_tool(svc, "wait_for_answer", {"session_id": sid})
    _turn, err = await call_tool(svc, "next_turn", {"session_id": sid,
                                                   "candidate_said": got["candidate_said"]})
    assert not err
    hold.clear()  # the page is still playing the first sentence
    lines = [{"member": "chairman", "text": "Pehli baat yeh hai. Doosri baat yeh hai ki aapne "
              "apne jawab mein koi bhi thos kaaran nahi diya, sirf ek ummeed rakhi. Teesri baat, "
              "aap apna khud ka anubhav bataiye."}]
    saying = asyncio.create_task(call_tool(svc, "say", {"session_id": sid, "lines": lines}))
    await asyncio.sleep(0.1)
    await page.send(json.dumps({"type": "barge_in"}))
    hold.set()
    said, err = await saying
    assert not err and said["interrupted"] is True
    assert said["said_before_interruption"].startswith("Pehli baat yeh hai.")
    assert "anubhav" in said["not_said"]
    await asyncio.sleep(0.1)
    assert any(e.get("type") == "interrupted" for e in seen)
    room = svc.voice.room(sid)
    assert room.state == "listening"
    await page.send(b"\x00\x01" * 800)  # their words, now heard
    await asyncio.sleep(0.05)
    room.listener.push("Sir, ek minute, main apni baat poori karna chahta hoon.")
    await page.send(json.dumps({"type": "turn_end"}))
    got, err = await call_tool(svc, "wait_for_answer", {"session_id": sid})
    assert not err and got["candidate_said"].startswith("Sir, ek minute")
    assert "anubhav" in got["interrupted_you"]["not_said"]


async def test_speaking_over_a_hand_off_skips_the_rest_of_the_turn(rig):
    svc, start, page, speech, _seen, hold = rig
    sid = start["session_id"]
    await opening(svc, start)
    room = svc.voice.room(sid)
    await room.set_state("thinking")
    hold.clear()
    assert room.prespeak([{"member": "chairman", "text": "Dhanyavaad. Doctor Iyer, please."}])
    await asyncio.sleep(0.05)
    await page.send(json.dumps({"type": "barge_in"}))
    hold.set()
    before = len(speech.spoken)
    said, err = await call_tool(svc, "say", {"session_id": sid, "lines": [
        {"member": "chairman", "text": "Dhanyavaad. Doctor Iyer, please."},
        {"member": "economist", "text": "Physics ka ek siddhant samjhaiye."}]})
    assert not err and said["interrupted"] is True and "siddhant" in said["not_said"]
    assert not any("siddhant" in x for x in speech.spoken[before:])  # never spoken over them
    assert room.state == "listening"


@pytest.mark.parametrize(("text", "ends"), [
    ("Can we stop here please?", True), ("Let's stop here.", True),
    ("That's enough for today.", True), ("Bas, yahin rukte hain.", True),
    ("यहीं रुकते हैं।", True),
    ("I will stop here because the point is made.", False),  # the end of an answer
    ("Can we stop the Centre from overriding states?", False),  # a question on the topic
])
def test_mitra_hears_a_plain_request_to_stop(text, ends):
    from mockboard.modes.partner.session import _request

    assert (_request(text) == "end") is ends
