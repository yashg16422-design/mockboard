"""The microphone and language check before a sitting (upgrade #2).

The candidate reads one short line in the language they chose. Prisma transcribes it with the
same locale the sitting will use, and the page shows what the board heard, which words matched,
and plain advice (speak closer, use headphones, try another language). It catches a muted or
wrong microphone and a locale that turns the candidate's speech into the wrong words, before the
sitting rather than in the middle of it.

Nothing is stored: the audio is streamed to Prisma and dropped, and the result goes back to the
page only. One check is a few seconds of Prisma; the server route counts it through access
admission like other paid utilities.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any

from mockboard.config import Settings
from mockboard.speech import gnani_protocol as g

__all__ = [
    "CHECK_LINES",
    "MAX_CHECK_SECONDS",
    "CheckResult",
    "lang_code_for",
    "score",
    "transcribe_once",
]

# One short, neutral line per interview language: no name, no number, nothing gendered.
CHECK_LINES: dict[str, str] = {
    "hinglish": "Mere zile mein saaf paani sabse badi chunauti hai.",
    "english": "Clean drinking water is the biggest challenge in my district.",
    "hindi": "मेरे ज़िले में साफ़ पानी सबसे बड़ी चुनौती है।",
    "tanglish": "என் மாவட்டத்தில் குடிநீர் தான் மிகப்பெரிய சவால்.",
}
# The same line in the other script. With `hi-IN` Prisma writes Hinglish speech in Devanagari
# (seen in the owner's own check, Nov 6), so a Latin-only comparison failed a clear reading. The
# board reads both scripts the same way (listener v3 onwards), so either script counts here.
ALT_LINES: dict[str, tuple[str, ...]] = {
    "hinglish": ("मेरे ज़िले में साफ़ पानी सबसे बड़ी चुनौती है।",),
    "hindi": ("Mere zile mein saaf paani sabse badi chunauti hai.",),
}
SCRIPT_NOTE = (
    "The board wrote your words in {script} script. That is expected with this language setting, "
    "and the board reads Hinglish the same way in either script."
)
MAX_CHECK_SECONDS = 10
MIN_SPEECH_RMS = 150  # 16-bit samples; below this the clip is treated as silence
WORD_MATCH = 0.75  # a heard word close enough to an expected word counts as matched
CLEAR_AT = 0.8
PARTLY_AT = 0.5

ADVICE = {
    "clear": "The board heard you clearly. You are ready to begin.",
    "partly": (
        "The board heard part of the line. Speak a little closer to the microphone and a little "
        "slower, then check again. Headphones help."
    ),
    "unclear": (
        "The board could not follow the line. Check that the right microphone is chosen, speak "
        "closer, and make sure the language you chose is the one you are speaking."
    ),
    "silent": (
        "No speech reached the board. Check that the microphone is allowed for this page and "
        "not muted, then try again."
    ),
    "error": (
        "The check could not reach the speech service just now. You can still begin; if the "
        "board asks you to repeat often, try the check again later."
    ),
}


def lang_code_for(language: str, settings: Settings) -> str:
    """The Prisma locale a sitting in this interview language uses (as `server.make_speech`).

    Checked live on generated speech (Nov 9, prisma-v2.5): `hi-IN` writes English in Devanagari
    ("कुड यू रिपीट दी क्वेस्शन"), so an English sitting missed every spoken request, while `en-IN`
    heard all five word for word; `en-IN` returned nothing for a Hinglish clip (Round 49). So the
    locale follows the language, whatever `STT_LANG_CODE` says. A Hindi-family override such as
    "hi-IN,en-IN" (the v3.0 A/B) is kept for Hinglish and Hindi."""
    if language == "tanglish":
        return "ta-IN"
    if language == "english":
        return "en-IN"
    configured = settings.stt_lang_code or ""
    return configured if configured.startswith("hi") else "hi-IN"


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).casefold()
    kept = "".join(
        ch if ch.isalnum() or ch.isspace() or unicodedata.category(ch)[0] == "M" else " "
        for ch in text
    )
    return kept.split()


def rms(pcm: bytes) -> float:
    """Root-mean-square level of 16-bit little-endian mono PCM."""
    if len(pcm) < 2:
        return 0.0
    count = len(pcm) // 2
    total = 0
    for i in range(0, count * 2, 2):
        sample = int.from_bytes(pcm[i : i + 2], "little", signed=True)
        total += sample * sample
    return (total / count) ** 0.5


@dataclass
class CheckResult:
    language: str
    expected: str
    heard: str
    verdict: str  # clear | partly | unclear | silent | error
    script_note: str = ""
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    match_ratio: float = 0.0
    advice: str = ""
    lang_code: str = ""
    latency_ms: int | None = None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def score(language: str, heard: str, *, lang_code: str = "", silent: bool = False,
          error: bool = False, latency_ms: int | None = None) -> CheckResult:
    """Compare what Prisma heard with the line the candidate read. Pure."""
    expected = CHECK_LINES[language]
    candidates = (expected, *ALT_LINES.get(language, ()))
    best = max(candidates, key=lambda line: _match(line, heard)[2])
    if error:
        verdict = "error"
    elif silent or not heard.strip():
        verdict = "silent"
    else:
        verdict = ""
    matched, missing, ratio = _match(best, heard)
    note = ""
    if heard.strip() and best != expected:
        note = SCRIPT_NOTE.format(script="Devanagari" if _DEVANAGARI.search(best) else "Latin")
    if not verdict:
        verdict = "clear" if ratio >= CLEAR_AT else "partly" if ratio >= PARTLY_AT else "unclear"
    return CheckResult(
        language=language, expected=expected, heard=heard.strip(), verdict=verdict,
        matched=matched, missing=missing, match_ratio=round(ratio, 2), advice=ADVICE[verdict],
        lang_code=lang_code, latency_ms=latency_ms, script_note=note,
    )


_DEVANAGARI = re.compile(r"[\u0900-\u097f]")


def _match(line: str, heard: str) -> tuple[list[str], list[str], float]:
    """Words of `line` heard (allowing small slips), words missed, and the share heard."""
    want = _words(line)
    pool = _words(heard)
    matched: list[str] = []
    missing: list[str] = []
    for word in want:
        best = max(pool, key=lambda h: SequenceMatcher(None, word, h).ratio(), default=None)
        if best is not None and SequenceMatcher(None, word, best).ratio() >= WORD_MATCH:
            matched.append(word)
            pool.remove(best)
        else:
            missing.append(word)
    return matched, missing, (len(matched) / len(want) if want else 0.0)


async def transcribe_once(
    pcm: bytes,
    settings: Settings,
    *,
    lang_code: str,
    api_key: str,
    url: str | None = None,
    realtime: bool = True,
    idle_timeout_s: float = 4.0,
    total_timeout_s: float = 25.0,
) -> tuple[str, int | None]:
    """Stream one clip to Prisma and return (final text, last-audio-to-final ms).

    Ends the clip with trailing silence so the server closes the utterance, then waits until the
    server has been quiet for `idle_timeout_s`. The socket is always closed. Raises on a
    connection or protocol error so the caller can report "error" honestly."""
    from websockets.asyncio.client import connect

    cfg = g.STTConfig.from_settings(settings, lang_code=lang_code)
    headers = g.build_stt_headers(cfg, api_key)
    rate = cfg.sample_rate
    silence = b"\x00" * (int(g.TRAILING_SILENCE_S * rate) * 2)
    speech_frames = list(g.iter_frames(pcm))
    frames = speech_frames + list(g.iter_frames(silence))
    finals: list[str] = []
    last_audio: float | None = None
    last_final: float | None = None

    async def run() -> None:
        nonlocal last_audio, last_final
        async with connect(url or settings.gnani_stt_url, additional_headers=headers,
                           max_size=None, open_timeout=10) as ws:
            first = g.parse_stt_message(await asyncio.wait_for(ws.recv(), 10))
            if first.type == "error":
                raise ConnectionError(str(first.raw.get("message", "speech service error")))
            sent = asyncio.Event()

            async def sender() -> None:
                nonlocal last_audio
                t0 = time.monotonic()
                for i, deadline in g.pace_frames(len(frames), rate, t0):
                    if realtime:
                        delay = deadline - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                    await ws.send(frames[i])
                    if i == len(speech_frames) - 1:
                        last_audio = time.monotonic()
                sent.set()

            async def receiver() -> None:
                nonlocal last_final
                quiet_since = time.monotonic()
                while True:
                    try:
                        data = await asyncio.wait_for(ws.recv(), 0.25)
                    except TimeoutError:
                        if sent.is_set() and time.monotonic() - quiet_since >= idle_timeout_s:
                            return
                        continue
                    msg = g.parse_stt_message(data)
                    quiet_since = msg.arrived
                    if msg.type == "error":
                        raise ConnectionError(str(msg.raw.get("message", "speech service error")))
                    if msg.type == "transcript" and not msg.is_partial and msg.text.strip():
                        finals.append(msg.text.strip())
                        last_final = msg.arrived

            send_task = asyncio.create_task(sender())
            try:
                await receiver()
            finally:
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task

    await asyncio.wait_for(run(), total_timeout_s)
    latency = None
    if last_audio is not None and last_final is not None:
        latency = max(0, round((last_final - last_audio) * 1000))
    return " ".join(finals).strip(), latency
