"""The owner's test of the Claude room (Nov 10): hearing, pronunciation, and the board cutting in.

1. The bridge dropped the last words when Prisma was slow on the last sentence: it now waits for
   the final of any speech sent after the last final.
2. The Hindi voices read Latin Hinglish badly: a line may carry `speak`, the same words in
   Devanagari, which Timbre reads while the caption keeps `text`.
3. The board waited for the end of an answer before pointing out something wrong: with
   `interim`, each new piece arrives while the person speaks, and a checked `cut_in` stops them.
Free: scripted speech, no Gnani, no model.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time

import pytest

from mockboard.mcp.service import BoardService, default_settings
from mockboard.mcp.tools import call_tool
from mockboard.mcp.voice_bridge import PrismaListener, VoiceBridge, _rms

websockets = pytest.importorskip("websockets")

FORM = {"home_state": "Uttar Pradesh", "district": "Lucknow", "degree_subject": "Physics",
        "hobbies": ["Chess"], "interview_language": "hinglish"}


class ScriptListener:
    def __init__(self) -> None:
        self.finals: list[str] = []
        self.on_final = None

    def push(self, text: str) -> None:  # a final from "Prisma"
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
        self.log: list[str] = []  # synth and played, in order
        self.listener_made = ScriptListener()

    async def synthesize(self, text, voice, language):
        self.spoken.append(text)
        self.log.append(f"synth {text}")
        return b"\x00\x00" * 160

    async def listener(self, lang_code):
        return self.listener_made


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def open_rig(tmp_path, tool="start_voice_interview", args=None):
    svc = BoardService()
    speech = ScriptSpeech()

    def make():
        bridge = VoiceBridge(default_settings(), lambda s: speech, free_port())
        bridge.log_dir = tmp_path
        return bridge

    svc.voice_factory = make
    start, err = await call_tool(svc, tool, args or {"form": FORM})
    assert not err, start
    page = await websockets.connect(start["voice"]["ws_url"])

    async def ack():
        head = None
        async for data in page:
            if isinstance(data, bytes):
                await asyncio.sleep(0.03)  # the piece plays
                speech.log.append(f"played {head['seq']}")
                await page.send(json.dumps({"type": "played", "seq": head["seq"]}))
            elif json.loads(data).get("type") == "audio":
                head = json.loads(data)

    reader = asyncio.create_task(ack())
    await page.send(json.dumps({"type": "ready"}))

    async def close():
        reader.cancel()
        await page.close()
        await svc.aclose()

    return (svc, start, page, speech, tmp_path), close


@pytest.fixture
async def rig(tmp_path):
    parts, close = await open_rig(tmp_path)
    yield parts
    await close()


async def speak_opening(svc, start):
    lines = [{"member": x["member"], "text": x.get("text") or x["draft"]} for x in start["say"]]
    out, err = await call_tool(svc, "say", {"session_id": start["session_id"], "lines": lines})
    assert not err and out["spoken"]


async def test_the_last_words_are_waited_for_not_dropped():
    lst = PrismaListener.__new__(PrismaListener)
    lst.cfg = type("C", (), {"sample_rate": 16000})()
    lst.finals, lst._buf, lst._recv, lst.ws, lst.on_final = ["pehla vaakya"], b"", None, None, None
    lst._last_final = time.monotonic() - 2.0
    lst._last_speech = time.monotonic() - 0.5  # speech after the last final: not yet transcribed

    async def late_final():
        await asyncio.sleep(0.6)
        lst.finals.append("aakhri shabd")
        lst._last_final = time.monotonic()

    task = asyncio.create_task(late_final())
    text = await lst.finish()
    await task
    assert text == "pehla vaakya aakhri shabd"


def test_speech_and_quiet_are_told_apart():
    loud = (b"\x00\x20" * 800)  # 16-bit samples of 8192
    assert _rms(loud) > 500 and _rms(b"\x00\x00" * 800) == 0


async def test_speak_is_what_timbre_reads_and_text_is_the_caption(rig):
    svc, start, _page, speech, logs = rig
    out, err = await call_tool(svc, "say", {"session_id": start["session_id"], "lines": [
        {"member": "chairman", "text": "Pehle apna parichay dijiye.",
         "speak": "पहले अपना परिचय दीजिए।"}]})
    assert not err and out["spoken"]
    assert "पहले अपना परिचय दीजिए।" in speech.spoken  # (the voices warm their "hmm" too)
    log = (logs / f"claude-{start['session_id']}.jsonl").read_text(encoding="utf-8")
    assert "Pehle apna parichay dijiye." in log and '"type": "prep"' in log


async def test_the_board_hears_the_answer_as_it_comes_and_cuts_in(rig):
    svc, start, page, speech, _ = rig
    sid = start["session_id"]
    await speak_opening(svc, start)
    await page.send(b"\x00\x01" * 800)  # the person starts speaking: the listener opens
    await asyncio.sleep(0.05)
    waiting = asyncio.create_task(call_tool(svc, "wait_for_answer",
                                            {"session_id": sid, "interim": True}))
    await asyncio.sleep(0.05)
    speech.listener_made.push("Main Lucknow se hoon aur physics padhi hai,")
    got, err = await waiting
    assert not err and got["status"] == "speaking" and "Lucknow" in got["heard_so_far"]
    speech.listener_made.push("aur Lucknow mein ek bhi school nahi hai sir.")

    refused, err = await call_tool(svc, "next_turn", {
        "session_id": sid, "candidate_said": "x", "cut_in": True,
        "proposal": {"action": "confront", "quote": "Lucknow has no hospitals", "reason": "x"}})
    assert err and "really heard" in refused["error"]

    out, err = await call_tool(svc, "next_turn", {
        "session_id": sid, "candidate_said": "x", "cut_in": True,
        "proposal": {"action": "confront", "quote": "ek bhi school nahi hai",
                     "reason": "an exaggerated claim about their own city"}})
    assert not err, out
    assert out["board"]["cut_in"] is True and out["board"]["rule"] == "M-CONFRONT"
    turns = svc.sessions[sid].session.state.turns
    assert turns[-1].transcript.endswith("ek bhi school nahi hai sir.")


async def test_a_cut_in_needs_enough_words_and_comes_once(rig):
    svc, start, page, speech, _ = rig
    sid = start["session_id"]
    await speak_opening(svc, start)
    await page.send(b"\x00\x01" * 800)
    await asyncio.sleep(0.05)
    speech.listener_made.push("Sir main")
    early, err = await call_tool(svc, "next_turn", {
        "session_id": sid, "candidate_said": "Sir main", "cut_in": True,
        "proposal": {"action": "elaborate", "reason": "x"}})
    assert err and "too little" in early["error"]
    no_prop, err = await call_tool(svc, "next_turn", {
        "session_id": sid, "candidate_said": "Sir main", "cut_in": True})
    assert err and "proposal" in no_prop["error"]


def test_the_room_has_an_end_button_and_hides_the_thinking_until_asked():
    from mockboard.mcp.voice_bridge import PAGE_PATH

    html = PAGE_PATH.read_text(encoding="utf-8")
    assert 'id="endBtn"' in html and "End interview" in html
    assert 'data-graph="off"' in html and 'id="graphBtn"' in html
    assert "const END_SILENCE_MS = 2200;" in html  # a thinking pause no longer ends the answer


def test_the_first_words_of_a_turn_go_alone_so_the_voice_starts_sooner():
    from mockboard.mcp.voice_bridge import speech_pieces

    long = ("Aapne kaha tha ki system ke andar rehkar badlav lana chahte hain. " * 2).strip()
    first = "Dhanyavaad. " + long
    assert speech_pieces(first, first=True)[0] == "Dhanyavaad."
    assert " ".join(speech_pieces(first, first=True)) == " ".join(speech_pieces(first))
    assert speech_pieces("Ek chhota sawaal. Doosra.", first=True) == ["Ek chhota sawaal. Doosra."]
    assert speech_pieces("पहला वाक्य। " + "दूसरा वाक्य बहुत लंबा है। " * 6, first=True)[0] == \
        "पहला वाक्य।"


async def test_the_next_piece_is_made_while_this_one_plays(rig):
    svc, start, _page, speech, _ = rig
    speech.log.clear()
    out, err = await call_tool(svc, "say", {"session_id": start["session_id"], "lines": [
        {"member": "chairman", "text": "Dhanyavaad. Doctor Iyer, please."},
        {"member": "economist", "text": "Namaste."}]})
    assert not err and out["spoken"]
    first_played = speech.log.index(next(x for x in speech.log if x.startswith("played")))
    assert [x.startswith("synth") for x in speech.log[:first_played]].count(True) >= 2


async def test_mitra_cuts_in_only_to_correct_a_wrong_fact(tmp_path):
    (svc, start, page, speech, _), close = await open_rig(
        tmp_path, "start_voice_partner", {"topic": "Federalism in India", "language": "english"})
    try:
        sid = start["session_id"]
        await speak_opening(svc, start)
        await page.send(b"\x00\x01" * 800)
        await asyncio.sleep(0.05)
        speech.listener_made.push("Federalism means the President appoints every Chief Minister "
                                  "directly, sir.")
        nudge, err = await call_tool(svc, "next_turn", {
            "session_id": sid, "candidate_said": "x", "cut_in": True,
            "proposal": {"action": "elaborate", "reason": "x"}})
        assert err and "only to correct a wrong fact" in nudge["error"]
        out, err = await call_tool(svc, "next_turn", {
            "session_id": sid, "candidate_said": "x", "cut_in": True,
            "proposal": {"action": "confront", "quote": "the President appoints every Chief "
                         "Minister", "reason": "wrong: the Governor appoints, by majority"}})
        assert not err, out
        assert out["board"]["cut_in"] is True
    finally:
        await close()


INTRO = ("Main Lucknow se hoon, physics padhi hai, aur ek saal school mein padhaya, jahan dekha ki "
         "ek niyam hazaron bachchon tak pahunchta hai.")
LONG = ("Main pehle sabse prabhavit logon se baat karunga, kyunki samasya woh sabse achhi jaante "
        "hain, phir laagat aur laabh tolunga.")


async def typed(svc, sid, page, text):
    await page.send(json.dumps({"type": "typed_answer", "text": text}))
    got, err = await call_tool(svc, "wait_for_answer", {"session_id": sid})
    assert not err and got["status"] == "answered", got
    return got["candidate_said"]


async def test_a_hand_off_is_spoken_at_the_decision_and_only_once(rig):
    svc, start, page, speech, _ = rig
    sid = start["session_id"]
    await speak_opening(svc, start)
    said = await typed(svc, sid, page, INTRO)
    out, err = await call_tool(svc, "next_turn", {"session_id": sid, "candidate_said": said})
    assert not err
    await call_tool(svc, "say", {"session_id": sid, "lines": [
        {"member": x["member"], "text": x.get("text") or x["draft"]} for x in out["say"]]})
    said = await typed(svc, sid, page, LONG)
    out, err = await call_tool(svc, "next_turn", {
        "session_id": sid, "candidate_said": said,
        "proposal": {"action": "pivot", "reason": "heard enough"}})
    assert not err and out["board"]["rule"] == "D-HANDOFF", out["board"]
    hand_off = out["say"][0]
    assert hand_off["how"] == "verbatim" and "being_spoken" in hand_off
    await asyncio.sleep(0.05)
    assert any(hand_off["text"].split(".")[0] in x for x in speech.spoken)  # before any say
    before = len(speech.spoken)
    lines = [{"member": x["member"], "text": x.get("text") or x["draft"]} for x in out["say"]]
    got, err = await call_tool(svc, "say", {"session_id": sid, "lines": lines})
    assert not err and got["spoken"]
    assert not any(hand_off["text"].split(".")[0] in x for x in speech.spoken[before:])
    assert svc.sessions[sid].session is not None


async def test_a_few_new_words_do_not_wake_the_ai(rig):
    svc, start, page, speech, _ = rig
    sid = start["session_id"]
    await speak_opening(svc, start)
    await page.send(b"\x00\x01" * 800)
    await asyncio.sleep(0.05)
    waiting = asyncio.create_task(call_tool(svc, "wait_for_answer",
                                            {"session_id": sid, "interim": True}))
    await asyncio.sleep(0.05)
    speech.listener_made.push("Sir main")
    await asyncio.sleep(0.1)
    assert not waiting.done()  # two words: not worth a look
    speech.listener_made.push("Lucknow se hoon aur maine physics padhi hai.")
    got, err = await waiting
    assert not err and got["status"] == "speaking" and got["heard_so_far"].startswith("Sir main")
