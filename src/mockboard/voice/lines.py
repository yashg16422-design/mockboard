"""Board lines in voice mode: sentence splitting (ADR-008 §2) and what was really heard
(ADR-008 §3). Pure Python: no Pipecat, so `make test` covers it on a plain checkout.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

# ADR-008 §2: a sentence ends at ".", "?", "!" or "।" followed by whitespace or the end of
# the stream. A full stop between two digits ("12.5") is never a boundary, which holds by
# construction: the character after the stop must be whitespace.
_BOUNDARY = re.compile(r"[.?!।]+(?=\s)")
_WORD = re.compile(r"\S+")
# Speaking time per word, for a cut unit whose audio has not all arrived. About 150 words a
# minute. UNVERIFIED for Timbre's Hinglish (no word timestamps; the trace marks it estimated).
EST_MS_PER_WORD = 400.0


def split_sentences(text: str) -> list[str]:
    """Split a complete line into sentences (the same rule as the streaming splitter)."""
    splitter = SentenceSplitter()
    out = splitter.push(text)
    tail = splitter.flush()
    return [*out, tail] if tail else out


class SentenceSplitter:
    """Streaming sentence splitter: text chunks in, complete sentences out.

    A boundary is confirmed only when the whitespace after it has arrived, so "12." followed
    by a chunk "5 lakh" stays one sentence. `flush()` returns whatever is left at the end."""

    def __init__(self) -> None:
        self._buf = ""

    def push(self, chunk: str) -> list[str]:
        self._buf += chunk
        out: list[str] = []
        while True:
            m = _BOUNDARY.search(self._buf)
            if m is None:
                break
            sentence = self._buf[: m.end()].strip()
            self._buf = self._buf[m.end() :]
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        return rest


def first_words(text: str, n: int) -> str:
    return " ".join(_WORD.findall(text)[: max(0, n)])


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


# ---------------------------------------------------------------------------
# What was heard (ADR-008 §3)
# ---------------------------------------------------------------------------


@dataclass
class Unit:
    """One Timbre request: its text and the audio received for it."""

    text: str
    requested_at: float
    audio_ms: float = 0.0
    first_audio_at: float | None = None
    done_at: float | None = None
    failed: bool = False


@dataclass
class Heard:
    """What the candidate heard of one board line."""

    text: str  # the spoken text (whole units, plus the estimated start of a cut unit)
    estimated: bool  # True when a cut unit's words were estimated from its audio share
    played_ms: float
    audio_ms: float
    units: list[dict] = field(default_factory=list)


class PlaybackLedger:
    """Per-line accounting of audio received from TTS and audio played to the candidate.

    The TTS service reports each request (`unit_started`), its audio (`audio`) and its end
    (`unit_done`); requests are serialised (one at a time, ruling 30), so audio always belongs
    to the latest started unit. The output transport's `BotStartedSpeakingFrame` /
    `BotStoppedSpeakingFrame` give the playback intervals. Audio plays in request order, so
    the first `played_ms` of the concatenated audio is what was heard (ADR-008 §3):

    * a unit whose audio ended within `played_ms` was spoken in full;
    * the unit being played is cut: its first floor(share x words) words count, where share is
      its played milliseconds over its received milliseconds (`estimated=True`);
    * later units were not spoken.

    Timbre gives no word timestamps, so the cut is an estimate, and the trace says so."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.units: list[Unit] = []
        self._speaking_since: float | None = None
        self._played_ms = 0.0
        self.first_audio_at: float | None = None
        self.playback_started_at: float | None = None
        self.last_activity: float | None = None

    def reset(self) -> None:
        """Start a new line. The same object is kept, because the TTS service holds it."""
        self.__init__(self._clock)

    # -- events from the TTS service --------------------------------------------------

    def unit_started(self, text: str) -> None:
        now = self._clock()
        self.units.append(Unit(text=text, requested_at=now))
        self.last_activity = now

    def audio(self, nbytes: int, sample_rate: int, num_channels: int = 1) -> None:
        if not self.units or nbytes <= 0:
            return
        now = self._clock()
        unit = self.units[-1]
        unit.audio_ms += nbytes / 2 / num_channels / sample_rate * 1000
        if unit.first_audio_at is None:
            unit.first_audio_at = now
        if self.first_audio_at is None:
            self.first_audio_at = now
        self.last_activity = now

    def unit_done(self, *, failed: bool = False) -> None:
        if self.units and self.units[-1].done_at is None:
            self.units[-1].done_at = self._clock()
            self.units[-1].failed = failed
        self.last_activity = self._clock()

    # -- events from the output transport ----------------------------------------------

    def bot_started(self) -> None:
        now = self._clock()
        if self._speaking_since is None:
            self._speaking_since = now
        if self.playback_started_at is None:
            self.playback_started_at = now
        self.last_activity = now

    def bot_stopped(self) -> None:
        now = self._clock()
        if self._speaking_since is not None:
            self._played_ms += (now - self._speaking_since) * 1000
            self._speaking_since = None
        self.last_activity = now

    # -- queries -----------------------------------------------------------------------

    @property
    def speaking(self) -> bool:
        return self._speaking_since is not None

    @property
    def audio_ms(self) -> float:
        return sum(u.audio_ms for u in self.units)

    @property
    def all_done(self) -> bool:
        return all(u.done_at is not None for u in self.units)

    def played_ms(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        open_ms = (now - self._speaking_since) * 1000 if self._speaking_since is not None else 0
        return min(self._played_ms + open_ms, self.audio_ms)

    def heard(self, now: float | None = None) -> Heard:
        played = self.played_ms(now)
        parts: list[str] = []
        estimated = False
        rows: list[dict] = []
        start = 0.0
        for u in self.units:
            end = start + u.audio_ms
            if u.audio_ms > 0 and end <= played + 1.0:  # 1 ms slack for float sums
                parts.append(u.text)
                share = 1.0
            elif u.audio_ms > 0 and start < played:
                # A unit still streaming has more audio to come, so its share of the words is
                # measured against an estimate of its full length (ruling 38: one request per
                # whole line makes this the common case).
                total = u.audio_ms if u.done_at is not None else max(
                    u.audio_ms, word_count(u.text) * EST_MS_PER_WORD)
                share = min(1.0, (played - start) / total)
                words = first_words(u.text, math.floor(share * word_count(u.text)))
                if words:
                    parts.append(words)
                estimated = True
            else:
                share = 0.0
            rows.append({"text": u.text, "audio_ms": round(u.audio_ms, 1),
                         "played_share": round(share, 3), "failed": u.failed})
            start = end
        return Heard(" ".join(parts).strip(), estimated, round(played, 1),
                     round(self.audio_ms, 1), rows)
