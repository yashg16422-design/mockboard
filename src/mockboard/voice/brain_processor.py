"""`BrainProcessor`: the board inside the Pipecat pipeline (ADR-007, V3; barge-in V4).

It sits between the user-turn processor and Timbre and talks to the brain only through the
session API the text CLI uses:

1. Every Prisma final (`TranscriptionFrame`) of the open user turn becomes
   `session.submit_segment(text, segment_index=k)`.
2. When Smart Turn ends the user's turn (`UserStoppedSpeakingFrame`), a turn task sends Prisma
   a `commit` (kept, but never relied on) and waits, bounded by `final_wait_s` (ruling 34), for
   Prisma's own final for the speech still in flight: until no segment is pending, no utterance
   is open, and every stretch of speech Silero heard has been transcribed. The first real run
   showed why: every commit replied "none" in ~80 ms and the finals came ~1.3 s after the
   candidate stopped. A commit that did close an utterance also waits for its tagged
   transcripts (ruling 31, `result["mb"]`). Then
   calls `session.end_of_turn(deadline)` and streams `session.speak_stream()` to Timbre as one
   `TTSSpeakFrame` per sentence (or, ruling 38, `TTS_REQUEST_UNIT=line`: one per whole guarded
   line; `first_rest`: the first sentence, then the rest as one). The turn task, not the stop
   frame's handler, does this work:
   a SystemFrame can overtake queued transcripts (spikes/NOTES.md, surprise 2), and blocking a
   SystemFrame handler would also block interruptions.
3. When the line has played (`BotStoppedSpeakingFrame` after the last audio), or the
   candidate cuts in, `session.finish_line(...)` records what was heard.

Ruling 26/31 safety: transcripts never start a turn (only Silero does, `strategies.py`). A
transcript caused by the commit of a turn whose move is already chosen (its wait timed out)
is a late final: it joins that turn's transcript (`note_late_final`) and never interrupts the
board. A transcript nobody's commit caused, arriving while the board has the floor and no user
turn is open (a cough under the barge-in threshold, or echo), is dropped and counted.

Barge-in (V4): a user turn that starts while the board is audible is an interruption: the turn
task (actor and TTS feed) is cancelled, Pipecat's `InterruptionFrame` stops Timbre (our V2
fix) and the output, and only the words actually heard are recorded (`PlaybackLedger`,
ADR-008 §3) with `interrupted=true`. A user turn that starts before the board's first audio
reopens the same turn instead (ruling 31).

The session opener (UPSC) or the Moderator's intro (debate) plays at start. The UPSC panel opens
with the Chairperson's fixed ritual line, spoken while the opening question is still being made
(`opening_ritual()` starts the agenda plan, `open()` runs under the ritual's audio). The debate's
"The board will confer" and verdict play at the end; the report goes to the page and to
`traces/`.

Time (panel): at the wrap-up (16 minutes) the session is asked to close with the next finished
turn, so the candidate is never cut off mid-answer; at the hard stop (17 minutes) a silent
candidate is closed at once with the Chairperson's time line, and one who is talking is treated
as having stopped. Debate keeps its single 10-minute stop. The page's `answers` and `closing`
events come from the session just before each line is spoken.

UI messages go to the browser as `OutputTransportMessageUrgentFrame`s
(`{"type": "mockboard", ...}`) over SmallWebRTC's data channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from mockboard.session import InterviewSession, TurnOutcome
from mockboard.speech.prisma_stt import PrismaProcessingFrame
from mockboard.voice.lines import PlaybackLedger, split_sentences
from mockboard.voice.strategies import IDLE, SPEAKING, THINKING

TTS_UNITS = ("sentence", "line", "first_rest")  # ruling 38, TTS_REQUEST_UNIT

# States of the conversation, as this processor sees them.
STARTING = "starting"  # the opener / Moderator intro
LISTENING = "listening"  # the board has finished; waiting for the candidate
USER = "user"  # a user turn is open
CLOSING = "closing"  # Smart Turn ended the turn; commit and wait for its transcripts
THINKING_S = "thinking"  # end_of_turn and the actor, no TTS yet
BOARD = "speaking"  # speak_stream is feeding TTS, or the line is playing
FINISHED = "finished"


class BrainProcessor(FrameProcessor):
    def __init__(
        self,
        session: InterviewSession,
        *,
        stt: Any = None,
        ledger: PlaybackLedger | None = None,
        tuner: Any = None,
        deadline_s: float | None = None,
        final_wait_s: float | None = None,
        settle_s: float = 0.12,
        tts_idle_cap_s: float = 9.0,
        line_cap_s: float = 90.0,
        orphan_close_s: float = 1.5,
        session_max_s: float | None = None,
        wrap_up_s: float | None = None,
        report_dir: Path | None = None,
        on_report: Callable[[Any, str], Awaitable[None]] | None = None,
        on_listen: Callable[[], Awaitable[None]] | None = None,
        autostart: bool = True,
        tts_request_unit: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.session = session
        self._stt = stt
        self.ledger = ledger or PlaybackLedger()
        self._tuner = tuner
        settings = session.deps.settings
        self._deadline_s = deadline_s if deadline_s is not None else (
            settings.move_deadline_ms / 1000
        )
        self._final_wait_s = final_wait_s if final_wait_s is not None else (
            settings.stt_final_wait_ms / 1000
        )
        # Ruling 38: one Timbre request per sentence, per whole line, or first + rest.
        self._tts_unit = tts_request_unit or getattr(settings, "tts_request_unit", "sentence")
        if self._tts_unit not in TTS_UNITS:
            raise ValueError(f"tts_request_unit must be one of {TTS_UNITS}")
        # Ruling 34: has Silero heard speech that no final has covered yet?
        self._vad_speaking = False
        self._speech_unfinalised = False
        self._await_late_final = False  # the wait ran out with speech still in flight
        self._settle_s = settle_s
        self._tts_idle_cap_s = tts_idle_cap_s
        self._line_cap_s = line_cap_s
        self._orphan_close_s = orphan_close_s
        self._session_max_s = session_max_s
        self._wrap_up_s = wrap_up_s
        self._report_dir = report_dir
        self._on_report = on_report
        self._on_listen = on_listen  # e.g. per-topic Prisma keyterms (ruling 27)
        # False: wait for `begin()` (the server calls it when the browser has connected, so
        # the opener's captions are not sent before the data channel exists).
        self._autostart = autostart

        self.state = STARTING
        self._task: asyncio.Task | None = None  # the turn task (or opener / closing lines)
        self._end_task: asyncio.Task | None = None
        self._clock_task: asyncio.Task | None = None
        self._open_task: asyncio.Task | None = None  # the panel's open(), running under the ritual
        self._orphan_task: asyncio.Task | None = None
        self._open_commits: set[int] = set()
        self._closed_commits: set[int] = set()
        self._seen_seq = 0
        self._seq_event = asyncio.Event()
        self._pending_marks: list[tuple[str, float | None]] = []
        self._last_vad_stop: float | None = None
        self._pushed = 0  # TTSSpeakFrames pushed for the current line
        self._time_up = False
        self._ending = False
        self._closing_sent = False  # the page has been told the interview is closing
        self.events: list[dict[str, Any]] = []
        self.outcomes: list[TurnOutcome] = []
        self.dropped: list[dict[str, Any]] = []
        self.commits: list[dict[str, Any]] = []
        self.report: Any = None
        self.report_text = ""

    # ------------------------------------------------------------------ for strategies

    def board_phase(self) -> str:
        """IDLE, THINKING or SPEAKING, for `BoardAwareStartStrategy` (V4 thresholds)."""
        if self.state in (CLOSING, THINKING_S):
            return THINKING
        if self.state in (BOARD, STARTING, FINISHED):
            return SPEAKING if self.ledger.playback_started_at is not None else THINKING
        return IDLE

    def thresholds_ms(self) -> tuple[int, int]:
        p = self.session.turn_params()
        return p.resume_min_speech_ms, p.barge_in_min_speech_ms

    # ------------------------------------------------------------------ frames

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            if self._autostart:
                await self.begin()
            return
        if isinstance(frame, EndFrame | CancelFrame):
            for task in (self._task, self._end_task, self._clock_task, self._orphan_task,
                         self._open_task):
                if task is not None:
                    await self.cancel_task(task)
            # A disconnect must not leave the panel's checkpointer thread behind.
            with contextlib.suppress(Exception):
                await self.session.close()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TranscriptionFrame):
            await self._on_transcript(frame)
            return
        if isinstance(frame, InterimTranscriptionFrame | PrismaProcessingFrame):
            return  # ours to read, not for TTS
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._last_vad_stop = time.monotonic()
            self._vad_speaking = False
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._last_vad_stop = None
            self._vad_speaking = True
            self._speech_unfinalised = True
            self._await_late_final = False  # new speech: a new turn's words from here on
        elif isinstance(frame, UserStartedSpeakingFrame):
            await self._on_user_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._on_user_stopped()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.ledger.bot_started()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.ledger.bot_stopped()
        elif isinstance(frame, InterruptionFrame):
            pass  # handled at UserStartedSpeakingFrame, which comes first; pass it on
        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------ UI messages

    async def emit(self, event: str, /, **fields: Any) -> None:
        msg = {"type": "mockboard", "event": event, "t": round(time.monotonic(), 3), **fields}
        self.events.append(msg)
        await self.push_frame(OutputTransportMessageUrgentFrame(message=msg))

    @staticmethod
    def _segment_field(segment: str) -> dict[str, str]:
        """The caption's `segment`, only when there is one (the panel's; debate captions are
        exactly what they were)."""
        return {"segment": segment} if segment else {}

    def events_of(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]

    async def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self.emit("state", state=state)

    # ------------------------------------------------------------------ transcripts

    async def _on_transcript(self, frame: TranscriptionFrame) -> None:
        mb = frame.result.get("mb") if isinstance(frame.result, dict) else None
        seq = mb.get("seq") if isinstance(mb, dict) else None
        cid = mb.get("commit_id") if isinstance(mb, dict) else None
        if isinstance(seq, int):
            self._seen_seq = max(self._seen_seq, seq)
        # A final covers the speech before it; speech still going on stays unfinalised.
        self._speech_unfinalised = self._vad_speaking
        self._seq_event.set()
        text = frame.text.strip()
        if not text or self.state == FINISHED:
            return
        turn = self.session.turn
        late = cid is not None and (
            cid in self._closed_commits
            or (cid in self._open_commits and turn is not None and turn.ended)
        )
        if not late and cid is None and self._await_late_final and turn is not None and (
                turn.ended and not self._vad_speaking):
            # Ruling 34: the final for this turn's last speech, after the wait ran out. It joins
            # the turn it belongs to and never starts a turn or interrupts the board.
            late = True
            self._await_late_final = False
        if late:
            # Ruling 26 safety net: caused by the commit of a turn whose move is chosen.
            self.session.note_late_final(text, commit_id=cid)
            await self.emit("caption", side="user", text=text, late=True,
                            **self._segment_field(self.session.current_segment()))
            return
        if self.state in (USER, CLOSING, LISTENING) or (cid is not None
                                                        and cid in self._open_commits):
            await self._submit(text)
            if self.state == LISTENING:
                self._watch_orphan()
            return
        # The board has the floor, no user turn is open, and no commit of ours caused it:
        # a cough under the barge-in threshold, or the board's own echo. Never a new turn.
        self.dropped.append({"text_words": len(text.split()), "state": self.state,
                             "commit_id": cid})
        await self.emit("transcript_dropped", words=len(text.split()), state=self.state)

    async def _submit(self, text: str) -> None:
        turn = self.session.turn
        index = turn.segments[-1].segment_index + 1 if turn and turn.segments else 0
        self.session.submit_segment(text, segment_index=index)
        self._flush_marks()
        self.session.mark(f"final_transcript_{len(self.session.turn.segments) - 1}")
        await self.emit("caption", side="user", text=text, turn=self.session.turn.turn_id,
                        **self._segment_field(self.session.current_segment()))

    def _mark(self, stage: str, at: float | None = None) -> None:
        if self.session.turn is not None:
            self.session.mark(stage, at)
        else:
            self._pending_marks.append((stage, at if at is not None else time.monotonic()))

    def _flush_marks(self) -> None:
        marks, self._pending_marks = self._pending_marks, []
        for stage, at in marks:
            self.session.mark(stage, at)

    def _watch_orphan(self) -> None:
        """A transcript arrived with no user turn open and the board silent (Silero missed the
        start). Close that turn ourselves if no user turn follows, so nothing waits forever."""
        if self._orphan_task is not None and not self._orphan_task.done():
            return

        async def close_later():
            await asyncio.sleep(self._orphan_close_s)
            if self.state == LISTENING and self.session.turn is not None:
                self._start_turn_task()

        self._orphan_task = self.create_task(close_later(), "brain-orphan")

    # ------------------------------------------------------------------ user turns

    async def _on_user_started(self) -> None:
        state = self.state
        if state == FINISHED:
            return
        if self._orphan_task is not None:
            await self.cancel_task(self._orphan_task)
            self._orphan_task = None
        turn = self.session.turn
        audible = self.ledger.playback_started_at is not None
        if state in (LISTENING, USER):
            pass
        elif state == CLOSING or (state == THINKING_S and (turn is None or turn.move is None)):
            # The candidate went on before the move was chosen: same turn, keep listening.
            await self._cancel_task()
            await self.emit("turn_resumed")
        elif state in (THINKING_S, BOARD) and not audible and turn is not None and turn.move:
            # Ruling 31: resumed speech before the board's first audio reopens the turn.
            await self._cancel_task()
            self.session.reopen_turn()
            self._reset_ledger()
            await self.emit("turn_reopened", turn=turn.turn_id)
        elif state in (THINKING_S, BOARD):
            await self._barge_in()
        elif state == STARTING:
            await self._cancel_task()  # the opener was cut off; it is already recorded
            await self.emit("line_end", interrupted=True, opener=True)
        await self._set_state(USER)

    def _on_user_stopped(self) -> None:
        if self.state == USER:
            self._start_turn_task()

    def _start_turn_task(self) -> None:
        self.state = CLOSING
        self._task = self.create_task(self._run_turn(), "brain-turn")

    async def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            await self.cancel_task(task)

    def _reset_ledger(self) -> PlaybackLedger:
        """A fresh ledger for the next line (the same object: the TTS service holds it)."""
        self.ledger.reset()
        self._pushed = 0
        return self.ledger

    def _settled(self, commit_task: asyncio.Task | None) -> bool:
        """Ruling 34: every word of the closed turn has arrived (or nothing is in flight)."""
        if self._speech_unfinalised:
            return False
        stt = self._stt
        if stt is not None and getattr(stt, "pending_segments", 0):
            return False
        if stt is not None and getattr(stt, "utterance_open", None) is True:
            return False
        if commit_task is not None and commit_task.done() and not commit_task.cancelled():
            result = commit_task.result()
            if result.last_seq is not None and self._seen_seq < result.last_seq:
                return False
        return True

    async def _run_turn(self) -> None:
        await self.emit("state", state=CLOSING)
        self._mark("vad_end", self._last_vad_stop)
        t0 = time.monotonic()
        commit_task = None
        if self._stt is not None and hasattr(self._stt, "commit"):
            # Kept (harmless, and it closes an open utterance at once when Prisma honours it),
            # but never waited on: its "none" reply means "pending", not "empty" (ruling 34).
            commit_task = asyncio.ensure_future(self._stt.commit())
            self._mark("commit_sent", t0)
        deadline = t0 + self._final_wait_s
        while not self._settled(commit_task) and time.monotonic() < deadline:
            self._seq_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._seq_event.wait(), 0.02)
        settled = self._settled(commit_task)
        self._await_late_final = not settled
        entry: dict[str, Any] = {"final_wait_ms": round((time.monotonic() - t0) * 1000, 1),
                                 "settled": settled}
        if commit_task is not None:
            if commit_task.done() and not commit_task.cancelled():
                r = commit_task.result()
                self._open_commits.add(r.commit_id)
                entry.update(commit_id=r.commit_id, reply=r.reply, wait_ms=r.wait_ms,
                             last_seq=r.last_seq)
            else:
                entry["reply"] = "pending"
                commit_task.add_done_callback(self._late_commit(entry))
        self.commits.append(entry)
        if self.session.turn is None:
            # Silero and Smart Turn heard something, but Prisma found no words in it.
            await self.emit("no_speech")
            await self._listen()
            return
        self._flush_marks()
        self._mark("turn_end")
        await self._set_state(THINKING_S)
        if self._open_task is not None and not self._open_task.done():
            await asyncio.shield(self._open_task)  # an answer given over the ritual: open first
        await self.session.end_of_turn(self._deadline_s)
        self._closed_commits |= self._open_commits
        self._open_commits.clear()
        await self._speak_turn()

    # ------------------------------------------------------------------ board lines

    def _late_commit(self, entry: dict[str, Any]):
        def done(task: asyncio.Task) -> None:
            if task.cancelled() or task.exception() is not None:
                return
            r = task.result()
            self._closed_commits.add(r.commit_id)  # its transcripts are late finals
            entry.update(commit_id=r.commit_id, reply=r.reply, wait_ms=r.wait_ms)
        return done

    async def _speak(self, text: str) -> None:
        self._pushed += 1
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    async def _feed_sentence(self, text: str, held: list[str]) -> None:
        """Ruling 38: send a guarded sentence now, or hold it for one request per line."""
        if self._tts_unit == "sentence" or (self._tts_unit == "first_rest" and not self._pushed):
            await self._speak(text)
        else:
            held.append(text)

    async def _speak_turn(self) -> None:
        self._reset_ledger()
        await self._set_state(BOARD)
        held: list[str] = []
        for event, fields in self.session.turn_events():  # `answers`, then `closing`
            if event == "closing":
                self._closing_sent = True
            await self.emit(event, **fields)
        async for sentence in self.session.speak_stream():
            await self.emit("caption", side="board", speaker=sentence.speaker,
                            move=sentence.move, text=sentence.text, kind=sentence.kind,
                            **self._segment_field(sentence.segment))
            await self._feed_sentence(sentence.text, held)
        if held:  # the whole guarded line (or its rest) as one Timbre request
            await self._speak(" ".join(held))
        await self._wait_played()
        self._mark_playback()
        out = self.session.finish_line(details=self._details())
        await self._after_line(out, interrupted=False)

    async def _barge_in(self) -> None:
        """V4: the candidate cut in while the board was audible."""
        heard = self.ledger.heard()
        await self._cancel_task()
        turn = self.session.turn
        if turn is None or turn.move is None:
            return
        self._mark_playback()
        out = self.session.finish_line(
            heard.text, interrupted=True, spoken_estimated=heard.estimated,
            details=self._details(heard),
        )
        await self._after_line(out, interrupted=True)

    def _mark_playback(self) -> None:
        if self.ledger.first_audio_at is not None:
            self.session.mark("tts_first_byte", self.ledger.first_audio_at)
        if self.ledger.playback_started_at is not None:
            self.session.mark("playback_start", self.ledger.playback_started_at)

    def _details(self, heard=None) -> dict[str, Any]:
        heard = heard or self.ledger.heard()
        return {
            "played_ms": heard.played_ms,
            "audio_ms": heard.audio_ms,
            "tts_units": heard.units,
            "commit": self.commits[-1] if self.commits else None,
            "transcripts_dropped": len(self.dropped),
            "stt_model": getattr(self._stt, "served_model", None),
        }

    async def _after_line(self, out: TurnOutcome, *, interrupted: bool) -> None:
        self.outcomes.append(out)
        voice = out.trace.get("voice", {})
        await self.emit("line_end", interrupted=interrupted, turn=out.turn_id,
                        heard=voice.get("board_line_spoken", ""), move=out.move.move.value)
        if out.finished or self._time_up:
            self._end_task = self.create_task(self._end_session(), "brain-end")
        elif not interrupted:
            await self._listen()

    async def _listen(self) -> None:
        if self._tuner is not None:
            self._tuner.apply(self.session.turn_params())
        if self._on_listen is not None:
            try:
                await self._on_listen()
            except Exception as e:  # context is a nicety; never stop the interview for it
                logger.warning(f"[brain] on_listen failed: {type(e).__name__}: {e}")
        await self._set_state(LISTENING)
        if self._time_up:
            self._end_task = self.create_task(self._end_session(), "brain-end")

    async def _wait_played(self) -> None:
        """Until every pushed sentence's audio has been received and played (or TTS gave
        nothing for a while, or the line cap), then a short settle."""
        start = time.monotonic()
        while True:
            now = time.monotonic()
            led = self.ledger
            last = led.last_activity or start
            if (len(led.units) >= self._pushed and led.all_done and not led.speaking
                    and now - last >= self._settle_s):
                return
            if not led.speaking and now - last > self._tts_idle_cap_s:
                logger.warning("[brain] TTS gave nothing for a while; ending the line")
                return
            if now - start > self._line_cap_s:
                return
            await asyncio.sleep(0.02)

    async def say(self, text: str, *, speaker: str, kind: str, segment: str = "") -> None:
        """A whole line that is not a turn's move (ritual, opener, confer, verdict)."""
        self._reset_ledger()
        held: list[str] = []
        for sentence in split_sentences(text):
            await self.emit("caption", side="board", speaker=speaker, text=sentence, kind=kind,
                            **self._segment_field(segment))
            await self._feed_sentence(sentence, held)
        if held:
            await self._speak(" ".join(held))
        await self._wait_played()

    @property
    def chairman(self) -> str:
        rt = self.session.deps.mode(self.session.mode)
        return self.session.speaker_name(rt.strategy.chairman)

    # ------------------------------------------------------------------ session start/end

    async def begin(self, delay_s: float = 0.0) -> None:
        """Start the conversation: the opener, then the candidate's first turn."""
        if self._task is not None or self.state != STARTING:
            return
        self._task = self.create_task(self._open_session(delay_s), "brain-open")
        if self._session_max_s or self._wrap_up_s:
            self._clock_task = self.create_task(self._session_timer(), "brain-timer")

    async def _open_session(self, delay_s: float = 0.0) -> None:
        if delay_s:
            await asyncio.sleep(delay_s)
        panel = {}
        members = self.session.panel_members()
        if members:  # the page labels its five seats before anyone speaks (WEB_REQUESTS.md)
            panel = {"members": members, "max_answers": self.session.max_answers()}
        await self.emit("session", mode=self.session.mode, session_id=self.session.session_id,
                        **panel)
        await self._set_state(STARTING)
        ritual = self.session.opening_ritual()  # starts the agenda plan; None in debate
        segment = self.session.current_segment()
        if not ritual:
            line = await self.session.open()
            await self.say(line.text, speaker=self.chairman, kind="opener", segment=segment)
        else:
            # The ritual is fixed text, so it plays while the plan and the opening question are
            # made: the seconds it takes are the opener's head start. `open()` is shielded, so a
            # candidate who talks over the ritual does not cancel it half way.
            self._open_task = asyncio.ensure_future(self.session.open())
            await self.say(ritual, speaker=self.chairman, kind="ritual", segment=segment)
            line = await asyncio.shield(self._open_task)
            text = line.text
            if text.startswith(ritual):
                text = text[len(ritual):].lstrip()
            if text:
                await self.say(text, speaker=self.chairman, kind="opener", segment=segment)
        if self.state == STARTING:
            await self._listen()

    async def _session_timer(self) -> None:
        """The wrap-up, then the hard stop (see the module docstring). Without a wrap-up (debate)
        the hard stop ends the session at once, as it always did."""
        wrap_up, hard = self._wrap_up_s, self._session_max_s
        if wrap_up and hard and wrap_up < hard:
            await asyncio.sleep(wrap_up)
            if self.session.request_wrap_up():
                await self.emit("wrap_up")
            await asyncio.sleep(hard - wrap_up)
        else:
            await asyncio.sleep(hard or wrap_up)
        self._time_up = True
        await self.emit("time_up")
        if self.state == LISTENING or (self.state == USER and not wrap_up):
            self._end_task = self.create_task(self._end_session(), "brain-end")
        elif self.state == USER:
            self._start_turn_task()  # a candidate still talking is treated as having stopped

    async def speech_lost(self, message: str) -> None:
        """Prisma gave up reconnecting (ruling 30): tell the candidate and end cleanly."""
        await self.emit("error", text="Speech recognition was lost; the session will end.",
                        detail=message[:200])
        if not self._ending:
            await self._cancel_task()
            self._end_task = self.create_task(self._end_session(), "brain-end")

    async def tts_failed(self, text: str) -> None:
        """Timbre gave no audio for a line (ruling 30): it is shown as text only."""
        await self.emit("tts_failed", text=text)

    # ------------------------------------------------------------------ stop (defect F)

    async def request_stop(self) -> None:
        """The page's Stop button (defect F, Round 19): end now. The turn in progress is
        closed as heard so far; if the candidate answered at least once, the judge runs and
        the report is shown and saved. Nothing more is spoken."""
        if self._ending:
            return
        await self.emit("stopping")
        await self._close_open_turn()
        if not self.session.state.turns:
            self._ending = True
            await self._set_state(FINISHED)
            await self.emit("ended", reason="no_answers",
                            text="You stopped before answering, so there is no report.")
            return
        self._end_task = self.create_task(self._end_session(speak=False), "brain-end")

    async def finish_without_page(self, timeout_s: float = 180.0) -> None:
        """The browser went away (tab closed, network gone): keep what was said, and if the
        candidate answered at least once, run the judge and save the report under traces/."""
        if self.report is not None:
            return
        end = self._end_task
        if end is not None and not end.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(end), timeout_s)
            return
        if self._ending:
            return
        await self._close_open_turn()
        if self.session.state.turns:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._end_session(speak=False), timeout_s)

    async def _close_open_turn(self) -> None:
        """Close the turn in progress honestly: a board line being played or generated is
        recorded as heard so far (interrupted); an answer whose move was not chosen yet is
        kept (the judge sees it) with nothing said by the board."""
        for task in (self._orphan_task,):
            if task is not None:
                await self.cancel_task(task)
        turn = self.session.turn
        if turn is not None and turn.move is not None:
            heard = self.ledger.heard()
            await self._cancel_task()
            self._mark_playback()
            details = {**self._details(heard), "stopped": True}
            out = self.session.finish_line(heard.text, interrupted=True,
                                           spoken_estimated=heard.estimated, details=details)
            self.outcomes.append(out)
            return
        await self._cancel_task()
        turn = self.session.turn
        if turn is not None and turn.segments:
            await self.session.end_of_turn(self._deadline_s)
            out = self.session.finish_line("", interrupted=True,
                                           details={"stopped": True, "board_silent": True})
            self.outcomes.append(out)

    async def _end_session(self, *, speak: bool = True) -> None:
        if self._ending:
            return
        self._ending = True
        await self._set_state(FINISHED)
        if speak and self._time_up and not self._closing_sent and self.session.panel_members():
            self._closing_sent = True  # time ran out with no close spoken yet
            await self.emit("closing", reason="time")
        confer = self.session.closing_line() if speak else None
        if confer:
            await self.say(confer, speaker=self.chairman, kind="confer")
        else:
            await self.emit("preparing_report")
        report = await self.session.finish()
        verdict = getattr(getattr(report, "verdict", None), "text", None)
        if verdict and speak:
            await self.say(verdict, speaker=self.chairman, kind="verdict")
        from mockboard.cli import format_report  # the text CLI's report layout

        self.report = report
        self.report_text = format_report(report, self.session.deps.cards).strip()
        path = None
        if self._report_dir is not None:
            self._report_dir.mkdir(parents=True, exist_ok=True)
            path = self._report_dir / f"{self.session.session_id}.report.txt"
            path.write_text(self.report_text + "\n", encoding="utf-8")
            path.with_suffix(".json").write_text(report.model_dump_json(indent=2),
                                                 encoding="utf-8")
        await self.emit("report", text=self.report_text, path=str(path) if path else None,
                        trace=str(self.session.recorder.path))
        if self._on_report is not None:
            await self._on_report(report, self.report_text)

