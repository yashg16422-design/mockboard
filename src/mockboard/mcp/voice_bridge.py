"""The Gnani voice room for the MCP channel (Option 2, CTO_NOTES_18 §8; owner, Nov 9).

The person's own AI is the brain; Mock Board's code decides every move (`service.py`); Gnani
Prisma listens and Gnani Timbre speaks, at our cost, through this bridge. It lives in the MCP
server's own process and listens on 127.0.0.1 only:

    GET  /room            the voice room page (also the MCP App view, `ui://mockboard/voice-room`)
    WS   /ws?s=<id>&k=<token>
         page -> bridge   binary: 16-bit mono PCM at the Prisma rate, only while the candidate
                          speaks; JSON: {"type": "hello"}, {"type": "turn_end"},
                          {"type": "typed_answer", "text": ...}, {"type": "played", "seq": n}
         bridge -> page   JSON events (state, caption, heard, prep, decision, finished, audio
                          headers) and binary Timbre PCM after each audio header

The AI drives a sitting with three tools: `say` (Timbre speaks each member's line in that
member's voice), `wait_for_answer` (returns what Prisma heard, or a typed answer) and the usual
`next_turn` (Mock Board's decision, which this bridge also draws as a node in the page's graph).

Every session has its own secret token; a page without it gets nothing. Audio is never stored.
`FakeSpeech` (no Gnani, free) serves tests and `MOCKBOARD_FAKE_SPEECH=1`.

UNVERIFIED: whether Claude Desktop shows this page as a panel and grants it the microphone is
what `docs/MCP_PROBE.md` measures; the same page also works in a normal browser tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from mockboard.config import DEFAULT_PANEL_VOICES, REPO_ROOT, Settings
from mockboard.speech import gnani_protocol as g

logger = logging.getLogger("mockboard.mcp.voice")

PAGE_PATH = Path(__file__).parent / "static" / "voice_room.html"
VIEW_URI = "ui://mockboard/voice-room.html"
APP_MIME = "text/html;profile=mcp-app"
FIRST_PORT = 8791
PORT_TRIES = 20
MAX_EVENTS = 400
MAX_TTS_CHARS = 380  # Timbre takes at most 400 characters a request
WAIT_LIMIT_S = 50.0  # a tool call returns before a host's timeout; the AI calls again
FINAL_WAIT_S = 3.5  # after the turn ends: at most this long for Prisma's last final transcript
QUIET_S = 0.3  # after that final, a moment for one more
SPEECH_RMS = 500  # 16-bit RMS above which a chunk holds speech, not the room's quiet
NUDGES_PER_ANSWER = 2
# The instant reaction when an answer ends: neutral sounds a listener makes, never approval.
FILLERS = {
    "hinglish": ["हम्म।", "जी।"], "hindi": ["हम्म।", "जी।"],
    "english": ["Hmm.", "I see."], "tanglish": ["Hmm.", "I see."],  # Tamil: UNVERIFIED, English
}
INTERIM_MIN_WORDS = 8  # an interim piece goes to the AI only with this many new words
READY_WAIT_S = 30.0  # the first `say` waits for the page's preparation animation to finish
ANSWER_MAX_CHARS = 4000
_SENTENCE = re.compile(r"(?<=[.?!।])\s+")


def load_settings() -> Settings:
    """The repository's own `.env` (the Gnani key), wherever Claude Desktop starts the server."""
    return Settings(_env_file=str(REPO_ROOT / ".env"))  # type: ignore[call-arg]


def gnani_key(settings: Settings) -> str:
    secret = settings.gnani_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if not key or key.startswith("replace-me"):
        raise RuntimeError("the Gnani key is not set in the repository's .env")
    return key


def _rms(pcm: bytes) -> float:
    """Root mean square of 16-bit mono PCM, from every fourth sample (cheap, good enough)."""
    import array

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    picked = samples[::4]
    if not picked:
        return 0.0
    return (sum(x * x for x in picked) / len(picked)) ** 0.5


def tts_chunks(text: str) -> list[str]:
    """Sentences packed into requests Timbre accepts (at most `MAX_TTS_CHARS` each)."""
    out: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        sentence = sentence.strip()
        while len(sentence) > MAX_TTS_CHARS:  # one very long sentence: cut at a space
            cut = sentence.rfind(" ", 0, MAX_TTS_CHARS)
            cut = cut if cut > 0 else MAX_TTS_CHARS
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if out and len(out[-1]) + 1 + len(sentence) <= MAX_TTS_CHARS:
            out[-1] = f"{out[-1]} {sentence}"
        else:
            out.append(sentence)
    return out


def _same(text: str) -> str:
    """A line's words without case or punctuation, to know a line already spoken."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


FIRST_PIECE_CHARS = 120  # the first words of a turn go alone, so the voice starts sooner


def speech_pieces(text: str, first: bool = False) -> list[str]:
    """`tts_chunks`, except that the first piece of a turn is a single short sentence: Timbre
    makes a short piece faster, and the rest is made while it plays."""
    chunks = tts_chunks(text)
    if not first or not chunks or len(chunks[0]) <= FIRST_PIECE_CHARS:
        return chunks
    sentences = [s.strip() for s in _SENTENCE.split(chunks[0]) if s.strip()]
    if len(sentences) < 2 or len(sentences[0]) > FIRST_PIECE_CHARS:
        return chunks
    return [sentences[0], " ".join(sentences[1:]), *chunks[1:]]


# --- speech ---------------------------------------------------------------------------------


class Listener(Protocol):
    async def feed(self, pcm: bytes) -> None: ...
    async def finish(self) -> str: ...
    async def close(self) -> None: ...


class Speech(Protocol):
    sample_rate: int

    async def synthesize(self, text: str, voice: str, language: str) -> bytes: ...
    async def listener(self, lang_code: str) -> Listener: ...


class PrismaListener:
    """One candidate turn on one Prisma socket: audio is streamed as it arrives, finals are
    collected, and `finish` sends trailing silence and waits briefly for the last final."""

    def __init__(self, settings: Settings, key: str, lang_code: str) -> None:
        self.settings = settings
        self.cfg = g.STTConfig.from_settings(settings, lang_code=lang_code)
        self.headers = g.build_stt_headers(self.cfg, key)
        self.finals: list[str] = []
        self.ws: Any = None
        self._buf = b""
        self._recv: asyncio.Task | None = None
        self._last_final = 0.0
        self._last_speech = 0.0  # when audio with speech in it was last sent
        self.on_final: Callable[[str], None] | None = None  # each new final, while still speaking

    async def connect(self) -> None:
        from websockets.asyncio.client import connect

        self.ws = await connect(self.settings.gnani_stt_url, additional_headers=self.headers,
                                max_size=None, open_timeout=10)
        first = g.parse_stt_message(await asyncio.wait_for(self.ws.recv(), 10))
        if first.type == "error":
            raise ConnectionError(str(first.raw.get("message", "speech service error")))
        self._recv = asyncio.create_task(self._receive())

    async def _receive(self) -> None:
        with contextlib.suppress(Exception):
            async for data in self.ws:
                msg = g.parse_stt_message(data)
                if msg.type == "transcript" and not msg.is_partial and msg.text.strip():
                    self.finals.append(msg.text.strip())
                    self._last_final = msg.arrived
                    if self.on_final is not None:
                        self.on_final(msg.text.strip())

    async def feed(self, pcm: bytes) -> None:
        if _rms(pcm) > SPEECH_RMS:
            self._last_speech = time.monotonic()
        self._buf += pcm
        whole = len(self._buf) - len(self._buf) % g.FRAME_BYTES
        for frame in g.iter_frames(self._buf[:whole]):
            await self.ws.send(frame)
        self._buf = self._buf[whole:]

    async def finish(self) -> str:
        silence = b"\x00" * (int(g.TRAILING_SILENCE_S * self.cfg.sample_rate) * 2)
        with contextlib.suppress(Exception):
            for frame in g.iter_frames(self._buf + silence):
                await self.ws.send(frame)
        self._buf = b""
        # Speech sent after Prisma's last final has not been transcribed yet: wait for its final.
        # Returning on older text alone dropped the candidate's last words (owner's test, Nov 10).
        deadline = time.monotonic() + FINAL_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            caught_up = self._last_final >= self._last_speech
            if caught_up and time.monotonic() - max(self._last_final, 0.0) > QUIET_S:
                break
        await self.close()
        return " ".join(self.finals).strip()

    def heard(self) -> str:
        return " ".join(self.finals).strip()

    async def close(self) -> None:
        if self._recv is not None:
            self._recv.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv
            self._recv = None
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None


class GnaniSpeech:
    """Gnani Prisma (`settings.stt_model`) and Gnani Timbre (`settings.tts_model`)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.key = gnani_key(settings)
        self.sample_rate = settings.tts_sample_rate

    async def synthesize(self, text: str, voice: str, language: str) -> bytes:
        from mockboard.speech.replay_speech import synthesize
        from mockboard.speech.tts_text import hinglish_for_tts

        devanagari = any("\u0900" <= ch <= "\u097f" for ch in text)
        spoken = hinglish_for_tts(text) if language == "hinglish" and not devanagari else text
        return await synthesize(spoken, voice, self.settings, api_key=self.key)

    async def listener(self, lang_code: str) -> Listener:
        lst = PrismaListener(self.settings, self.key, lang_code)
        await lst.connect()
        return lst


class _FakeListener:
    async def feed(self, pcm: bytes) -> None:
        return None

    async def finish(self) -> str:
        return ""

    async def close(self) -> None:
        return None


class FakeSpeech:
    """No Gnani: a short tone per line, and no transcript (answers are typed). Free."""

    sample_rate = 16000

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []

    async def synthesize(self, text: str, voice: str, language: str) -> bytes:
        self.spoken.append((voice, text))
        return b"\x00\x00" * int(0.02 * self.sample_rate * max(1, len(text.split())))

    async def listener(self, lang_code: str) -> Listener:
        return _FakeListener()


# --- one room -------------------------------------------------------------------------------


@dataclass
class VoiceRoom:
    session_id: str
    token: str
    language: str
    lang_code: str
    voices: dict[str, str]
    names: dict[str, str]
    speech: Speech
    pages: set[Any] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    state: str = "waiting"
    answer: asyncio.Future | None = None
    listener: Listener | None = None
    listening_since: float = 0.0
    seq: int = 0
    log_path: Path | None = None  # the Claude room's local log (traces/, never committed)
    played: dict[int, asyncio.Event] = field(default_factory=dict)
    finished: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # the page's preparation is drawn
    segment: asyncio.Event = field(default_factory=asyncio.Event)  # a new final while speaking
    interim_seen: int = 0  # finals already returned to the AI during this answer
    prespeaking: asyncio.Future | None = None  # fixed lines started at the board's decision
    prespoken: set[tuple[str, str]] = field(default_factory=set)
    cut_this_answer: bool = False
    # The person may talk over the board (owner, Nov 10: "even if I interrupt him while it is
    # speaking he should keep that in mind"): playing stops and what was left unsaid is kept.
    stop_play: bool = False
    playing: list[str] = field(default_factory=list)  # the pieces being spoken now
    play_index: int = -1
    interruption: dict[str, str] | None = None
    last_member: str | None = None
    fillers: dict[tuple[str, str], bytes] = field(default_factory=dict)  # instant reactions
    filler_turn: int = 0
    warming: asyncio.Future | None = None
    nudges_this_answer: int = 0

    # -- to the page --------------------------------------------------------------------

    async def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        del self.events[:-MAX_EVENTS]
        if self.log_path is not None:
            with contextlib.suppress(OSError), self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": round(time.time(), 3), **event},
                                    ensure_ascii=False) + "\n")
        await self._send(json.dumps(event, ensure_ascii=False))

    async def _send(self, data: str | bytes) -> None:
        for ws in list(self.pages):
            try:
                await ws.send(data)
            except Exception:  # a closed tab
                self.pages.discard(ws)

    async def set_state(self, state: str) -> None:
        self.state = state
        await self.publish({"type": "state", "state": state})

    # -- the AI's tools -----------------------------------------------------------------

    async def say(self, lines: list[dict[str, str]]) -> dict[str, Any]:
        """Speak each line in its member's voice; returns when the page has played them."""
        if not self.pages:
            for line in lines:
                await self.publish({"type": "caption", "side": "board", **line})
            return {"spoken": False, "reason": "the voice room is not open yet: ask the person to "
                    "open it (the panel, or voice_url in a browser), then call say again"}
        if not self.ready.is_set():  # let the preparation graph finish before the first word
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.ready.wait(), READY_WAIT_S)
        total_ms = 0
        if self.prespeaking is not None:  # the board's fixed lines, started at its decision
            with contextlib.suppress(Exception):
                total_ms += await self.prespeaking
            self.prespeaking = None
        done_already, self.prespoken = self.prespoken, set()
        rest = [x for x in lines if (x["member"], _same(x["text"])) not in done_already]
        if self.interruption is not None:  # they talked over the fixed lines: listen, not speak
            unsaid = " ".join(x["text"] for x in rest)
            self.interruption["not_said"] = " ".join(
                t for t in (self.interruption.get("not_said", ""), unsaid) if t)
            return self._interrupted_result(len(lines), total_ms)
        self.stop_play = False
        await self.set_state("speaking")
        total_ms += await self._play(rest)
        if self.interruption is not None:
            return self._interrupted_result(len(lines), total_ms)
        await self._start_listening()
        return {"spoken": True, "lines": len(lines), "audio_ms": total_ms}

    def _interrupted_result(self, n: int, total_ms: int) -> dict[str, Any]:
        return {"spoken": True, "lines": n, "audio_ms": total_ms, "interrupted": True,
                **(self.interruption or {}),
                "note": "the person spoke over you and the room is listening to them now: call "
                        "wait_for_answer. Answer what they said; pick up what you did not say "
                        "only if it still matters."}

    def prespeak(self, lines: list[dict[str, str]]) -> bool:
        """Start the board's fixed lines (a hand-off, the close) the moment the board decides,
        while the AI words the next question (owner: latency, Nov 10). `say` skips them."""
        if not self.pages or not self.ready.is_set() or self.state == "listening" or not lines:
            return False
        if self.prespeaking is not None:
            return False

        async def run() -> int:
            self.stop_play = False
            await self.set_state("speaking")
            return await self._play(lines)

        self.prespoken = {(x["member"], _same(x["text"])) for x in lines}
        self.prespeaking = asyncio.ensure_future(run())
        return True

    async def _play(self, lines: list[dict[str, str]]) -> int:
        """Speak the lines in order; returns the audio's length in ms once all of it played."""
        # Every piece of speech in order: (member, caption for the line's first piece, words).
        # The Hindi voices read Devanagari like a Hindi speaker; Latin Hinglish they mangle
        # (owner's test, Nov 10). The client sends the same words in Devanagari as `speak`.
        jobs: list[tuple[str, str | None, str]] = []
        for line in lines:
            pieces = speech_pieces(line.get("speak") or line["text"], first=not jobs)
            jobs += [(line["member"], line["text"] if i == 0 else None, p)
                     for i, p in enumerate(pieces)]

        def synth(job: tuple[str, str | None, str]) -> asyncio.Task:
            voice = self.voices.get(job[0]) or next(iter(self.voices.values()))
            return asyncio.ensure_future(self.speech.synthesize(job[2], voice, self.language))

        # The next piece is made while this one plays (owner: latency, Nov 10), so there is no
        # silence between a hand-off and the next member's question while Timbre works.
        total_ms, task = 0, synth(jobs[0]) if jobs else None
        self.playing, self.play_index = [j[2] for j in jobs], -1
        if jobs and self.warming is None:  # each voice's short reactions, made once, in advance
            self.warming = asyncio.ensure_future(self._warm_fillers())
        try:
            for i, (member, caption, _words) in enumerate(jobs):
                assert task is not None
                pcm = await task
                if self.stop_play:
                    break
                self.play_index, self.last_member = i, member
                task = synth(jobs[i + 1]) if i + 1 < len(jobs) else None
                if caption is not None:
                    await self.publish({"type": "caption", "side": "board", "member": member,
                                        "speaker": self.names.get(member, member),
                                        "text": caption})
                self.seq += 1
                seq = self.seq
                done = self.played[seq] = asyncio.Event()
                await self._send(json.dumps({"type": "audio", "seq": seq, "member": member,
                                             "rate": self.speech.sample_rate}))
                await self._send(pcm)
                ms = int(len(pcm) / 2 / self.speech.sample_rate * 1000)
                total_ms += ms
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(done.wait(), ms / 1000 + 4.0)
                self.played.pop(seq, None)
                if self.stop_play:
                    break
        finally:
            if task is not None and not task.done():
                task.cancel()
            self.playing, self.play_index = [], -1
        return total_ms

    # -- talking over the board, reacting at once, nudging ----------------------------------

    async def on_barge_in(self) -> None:
        """The person started speaking while the board spoke: stop at once and listen. What
        was said and what was left unsaid go to the AI with the answer."""
        if self.state != "speaking" or self.stop_play:
            return
        self.stop_play = True
        i = self.play_index
        said = " ".join(self.playing[: i + 1]) if i >= 0 else ""
        self.interruption = {"said_before_interruption": said,
                             "not_said": " ".join(self.playing[i + 1:])}
        for event in list(self.played.values()):
            event.set()
        await self.publish({"type": "interrupted"})
        await self._start_listening()

    def _filler_text(self) -> list[str]:
        return FILLERS.get(self.language, FILLERS["english"])

    async def _warm_fillers(self) -> None:
        for voice in dict.fromkeys(self.voices.values()):
            for text in self._filler_text():
                if (voice, text) in self.fillers:
                    continue
                try:
                    self.fillers[(voice, text)] = await self.speech.synthesize(
                        text, voice, self.language)
                except Exception as exc:  # no reaction is better than a late one
                    logger.info("voice: a reaction was not made: %s", type(exc).__name__)

    async def _react(self) -> None:
        """A short "hmm" / "ji" in the last speaker's voice the moment the answer ends, so the
        person is not left in silence while the board thinks (owner: latency, Nov 10)."""
        member = self.last_member
        voice = self.voices.get(member or "") if member else None
        if voice is None or not self.pages:
            return
        options = self._filler_text()
        text = options[self.filler_turn % len(options)]
        self.filler_turn += 1
        pcm = self.fillers.get((voice, text))
        if pcm is None:  # not made yet: stay silent rather than answer late
            return
        self.seq += 1
        await self._send(json.dumps({"type": "audio", "seq": self.seq, "member": member,
                                     "rate": self.speech.sample_rate, "reaction": True}))
        await self._send(pcm)

    async def nudge(self, member: str, text: str, speak: str | None = None) -> dict[str, Any]:
        """A few words while the person goes on speaking ("point par aaiye", "aur?"): played
        without ending their answer; the page does not send the microphone meanwhile."""
        if self.state != "listening" or self.listener is None:
            raise ValueError("the person is not speaking now")
        if self.nudges_this_answer >= NUDGES_PER_ANSWER:
            raise ValueError("two nudges in this answer already; let them finish")
        self.nudges_this_answer += 1
        voice = self.voices.get(member) or next(iter(self.voices.values()))
        pcm = await self.speech.synthesize(speak or text, voice, self.language)
        await self.publish({"type": "caption", "side": "board", "member": member,
                            "speaker": self.names.get(member, member), "text": text,
                            "nudge": True})
        self.seq += 1
        await self._send(json.dumps({"type": "audio", "seq": self.seq, "member": member,
                                     "rate": self.speech.sample_rate, "nudge": True}))
        await self._send(pcm)
        return {"nudged": True, "text": text}

    async def _start_listening(self) -> None:
        loop = asyncio.get_running_loop()
        if self.answer is None or self.answer.done():
            self.answer = loop.create_future()
        self.listening_since = time.monotonic()
        self.interim_seen, self.cut_this_answer, self.nudges_this_answer = 0, False, 0
        self.segment.clear()
        await self.set_state("listening")

    def heard_so_far(self) -> str:
        lst = self.listener
        return lst.heard() if lst is not None and hasattr(lst, "heard") else ""

    async def wait_for_answer(self, timeout_s: float, interim: bool = False) -> dict[str, Any]:
        """The answer when the person finishes; with `interim`, also each new piece of it while
        they are still speaking (Prisma closes a segment at each short pause), so the AI can
        decide to cut in."""
        if self.answer is None:
            if self.prespeaking is not None:  # the board's fixed lines are still playing
                with contextlib.suppress(Exception):
                    await self.prespeaking
                self.prespeaking, self.prespoken = None, set()
            await self._start_listening()
        assert self.answer is not None
        deadline = time.monotonic() + timeout_s
        finals: list[str] = []
        while True:
            waits = [asyncio.ensure_future(asyncio.shield(self.answer))]
            if interim:
                self.segment.clear()
                waits.append(asyncio.ensure_future(self.segment.wait()))
            _done, pending = await asyncio.wait(waits, timeout=max(0.0, deadline -
                                                                   time.monotonic()),
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if self.answer.done():
                got = self.answer.result()
                self.answer = None
                return got
            finals = list(getattr(self.listener, "finals", []) or [])
            new_words = len(" ".join(finals[self.interim_seen:]).split())
            # Each look costs the AI a turn, during which the person may finish: a piece goes
            # out only with enough new words to judge (owner: latency, Nov 10).
            if not interim or new_words >= INTERIM_MIN_WORDS or time.monotonic() >= deadline:
                break
        if interim and len(finals) > self.interim_seen:
            new = " ".join(finals[self.interim_seen:])
            self.interim_seen = len(finals)
            return {"status": "speaking", "heard_so_far": " ".join(finals), "new": new,
                    "note": "they are still speaking: cut in only for something a real board "
                            "would not let pass, else call wait_for_answer again"}
        return {"status": "waiting", "note": "no answer yet: call wait_for_answer again"}

    async def cut_in(self) -> str:
        """Stop listening now: what Prisma has heard so far is the answer. Returns it."""
        if self.state != "listening" or self.listener is None:
            raise ValueError("the person is not speaking now")
        text = self.listener.heard() if hasattr(self.listener, "heard") else ""
        await self.listener.close()
        self.listener = None
        self.cut_this_answer = True
        await self.set_state("thinking")
        await self.publish({"type": "cut_in", "text": "The board cuts in."})
        await self.publish({"type": "heard", "text": text, "source": "voice, cut in"})
        if self.answer is not None and not self.answer.done():
            self.answer.set_result({"status": "cut_in", "candidate_said": text, "source": "voice"})
        self.answer = None
        return text

    # -- from the page ------------------------------------------------------------------

    async def on_audio(self, pcm: bytes) -> None:
        if self.state != "listening":
            return  # the board is speaking: half duplex, so its own voice is never heard
        if self.listener is None:
            try:
                self.listener = await self.speech.listener(self.lang_code)
                if hasattr(self.listener, "on_final"):
                    self.listener.on_final = lambda _text: self.segment.set()
            except Exception as exc:
                logger.warning("voice: Prisma did not open: %s", type(exc).__name__)
                await self.publish({"type": "problem", "text": "Speech recognition did not "
                                    "start. You can type your answer instead."})
                return
        await self.listener.feed(pcm)

    async def on_turn_end(self) -> None:
        if self.state != "listening":
            return
        await self.set_state("thinking")
        await self._react()
        text = ""
        if self.listener is not None:
            try:
                text = await self.listener.finish()
            finally:
                self.listener = None
        await self._answer(text, "voice")

    async def on_typed(self, text: str) -> None:
        if self.listener is not None:
            await self.listener.close()
            self.listener = None
        await self.set_state("thinking")
        await self._answer(text.strip()[:ANSWER_MAX_CHARS], "typed")

    async def _answer(self, text: str, source: str) -> None:
        await self.publish({"type": "heard", "text": text, "source": source})
        result = {"status": "answered" if text else "silence", "candidate_said": text,
                  "source": source,
                  "spoke_after_ms": int((time.monotonic() - self.listening_since) * 1000)}
        if self.interruption is not None:  # this answer talked over the board
            result["interrupted_you"] = self.interruption
            self.interruption = None
        if self.answer is not None and not self.answer.done():
            self.answer.set_result(result)

    async def on_played(self, seq: int) -> None:
        event = self.played.get(seq)
        if event is not None:
            event.set()

    async def close(self) -> None:
        if self.listener is not None:
            await self.listener.close()
            self.listener = None
        for ws in list(self.pages):
            with contextlib.suppress(Exception):
                await ws.close()
        self.pages.clear()


# --- the server -----------------------------------------------------------------------------


class VoiceBridge:
    """All voice rooms of one MCP server process, on one 127.0.0.1 port."""

    def __init__(
        self,
        settings: Settings | None = None,
        speech_factory: Callable[[Settings], Speech] | None = None,
        port: int | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        fake = os.environ.get("MOCKBOARD_FAKE_SPEECH") == "1"
        self._factory = speech_factory or ((lambda s: FakeSpeech()) if fake else GnaniSpeech)
        self._speech: Speech | None = None
        self.port = port or 0
        self.rooms: dict[str, VoiceRoom] = {}
        self.latest: str | None = None
        self.log_dir: Path | None = None  # set by the Claude room; the MCP channel keeps no file
        self._server: Any = None

    @property
    def speech(self) -> Speech:
        if self._speech is None:
            self._speech = self._factory(self.settings)
        return self._speech

    async def start(self) -> None:
        if self._server is not None:
            return
        from websockets.asyncio.server import serve

        want = self.port or int(os.environ.get("MOCKBOARD_VOICE_PORT", FIRST_PORT))
        for port in range(want, want + PORT_TRIES):
            try:
                self._server = await serve(self._handle, "127.0.0.1", port,
                                           process_request=self._http, max_size=2**22)
            except OSError:
                continue
            self.port = port
            logger.warning("voice room on http://127.0.0.1:%s/room", port)
            return
        raise RuntimeError("no free port on 127.0.0.1 for the voice room")

    async def aclose(self) -> None:
        for room in list(self.rooms.values()):
            await room.close()
        self.rooms.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def view_meta(self) -> dict[str, Any]:
        origins = [f"{scheme}://{host}:{self.port}" for scheme in ("http", "ws")
                   for host in ("127.0.0.1", "localhost")]
        return {"ui": {"csp": {"connectDomains": origins}, "permissions": {"microphone": {}},
                       "prefersBorder": True}}

    async def open_room(self, session_id: str, language: str, lang_code: str,
                        names: dict[str, str]) -> VoiceRoom:
        await self.start()
        _ = self.speech  # fails here, before the sitting, if the Gnani key is missing
        table = self.settings.panel_voices() or DEFAULT_PANEL_VOICES.get(
            self.settings.tts_model, {})
        voices = {m: table.get(m, self.settings.tts_voice) for m in names}
        room = VoiceRoom(session_id, secrets.token_urlsafe(16), language, lang_code, voices,
                         names, self.speech)
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            room.log_path = self.log_dir / f"claude-{session_id}.jsonl"
        self.rooms[session_id] = room
        self.latest = session_id
        return room

    def room(self, session_id: str) -> VoiceRoom | None:
        return self.rooms.get(session_id)

    async def drop(self, session_id: str) -> None:
        room = self.rooms.pop(session_id, None)
        if room is not None:
            await room.close()

    def ticket(self, room: VoiceRoom) -> dict[str, Any]:
        base = f"127.0.0.1:{self.port}"
        query = f"s={room.session_id}&k={room.token}"
        return {"session_id": room.session_id, "voice_url": f"http://{base}/room?{query}",
                "ws_url": f"ws://{base}/ws?{query}"}

    # -- HTTP and WebSocket --------------------------------------------------------------

    def _http(self, connection: Any, request: Any) -> Any:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        path = urlparse(request.path).path
        if path == "/ws":
            return None  # go on to the WebSocket handshake
        if path in ("/", "/room"):
            body = page_html(self.port).encode("utf-8")
            headers = Headers([("Content-Type", "text/html; charset=utf-8"),
                               ("Cache-Control", "no-store"),
                               ("Content-Length", str(len(body)))])
            return Response(200, "OK", headers, body)
        body = b"not found"
        return Response(404, "Not Found", Headers([("Content-Length", str(len(body)))]), body)

    async def _handle(self, ws: Any) -> None:
        query = parse_qs(urlparse(ws.request.path).query)
        sid = (query.get("s") or [""])[0]
        token = (query.get("k") or [""])[0]
        room = self.rooms.get(sid)
        if room is None or not secrets.compare_digest(token, room.token):
            await ws.close(4403, "no such room")
            return
        room.pages.add(ws)
        try:
            for event in room.events:  # a page that opens late still sees the whole sitting
                await ws.send(json.dumps(event, ensure_ascii=False))
            await ws.send(json.dumps({"type": "state", "state": room.state}))
            async for data in ws:
                if isinstance(data, bytes):
                    await room.on_audio(data)
                    continue
                await self._on_text(room, data)
        except Exception:  # a dropped connection ends this page only
            pass
        finally:
            room.pages.discard(ws)

    async def _on_text(self, room: VoiceRoom, data: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "turn_end":
            await room.on_turn_end()
        elif kind == "typed_answer" and isinstance(msg.get("text"), str):
            await room.on_typed(msg["text"])
        elif kind == "played" and isinstance(msg.get("seq"), int):
            await room.on_played(msg["seq"])
        elif kind == "ready":
            room.ready.set()
        elif kind == "barge_in":
            await room.on_barge_in()


# The web app's own look, built into the page so it is the same in a Claude panel (where the page
# cannot load files) and in a browser tab: its tokens, its particle field, its fonts (SIL OFL 1.1).
WEB_STATIC = Path(__file__).resolve().parents[1] / "voice" / "static"
_FONTS = (  # (family, weights, file)
    ("Geist", "200 500", "geist-latin.woff2"),
    ("Geist Mono", "400 500", "geist-mono-latin.woff2"),
    ("Noto Sans Devanagari", "300 600", "noto-sans-devanagari.woff2"),
)
_PAGE_CACHE: dict[tuple[str, int], str] = {}


def _font_faces() -> str:
    import base64

    out = []
    for family, weights, name in _FONTS:
        path = WEB_STATIC / "fonts" / name
        if not path.exists():
            continue
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        out.append(f'@font-face {{ font-family: "{family}"; font-weight: {weights}; '
                   f'font-display: swap; src: url(data:font/woff2;base64,{data}) '
                   f'format("woff2"); }}')
    return "\n".join(out)


def build_page(template: Path, port: int) -> str:
    """A page with the web app's tokens, fonts and particle field inside it (no file to load)."""
    key = (str(template), port)
    if key not in _PAGE_CACHE:
        html = template.read_text(encoding="utf-8")
        tokens = (WEB_STATIC / "tokens.css").read_text(encoding="utf-8")
        presence = (WEB_STATIC / "presence.js").read_text(encoding="utf-8")
        html = html.replace("/*__TOKENS__*/", tokens + "\n" + _font_faces())
        html = html.replace("//__PRESENCE__", presence.replace("</script", "<\\/script"))
        _PAGE_CACHE[key] = html.replace("__PORT__", str(port))
    return _PAGE_CACHE[key]


def page_html(port: int) -> str:
    """The voice room page."""
    return build_page(PAGE_PATH, port)
