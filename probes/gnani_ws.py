"""Gnani wire-protocol details for the probes, kept in one place.

Every header name, message shape and framing rule below was copied from Gnani's docs,
fetched on Oct 25, 2026:

* STT (Prisma) WebSocket: https://docs.gnani.ai/api/STT/stt-websocket.md
* TTS (Timbre) WebSocket: https://docs.gnani.ai/api/TTS/tts-websocket.md
* TTS (Timbre) SSE:       https://docs.gnani.ai/api/TTS/tts-sse.md
* Voice catalog:          https://docs.gnani.ai/api/TTS/available-voices.md

Anything the docs leave unclear is marked `# UNVERIFIED:`. This module does no network
I/O itself (apart from helpers that receive an already-open connection), so the offline
self-test (`python -m probes.selftest`) can exercise all of it.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Settings: reuse the app's config instead of duplicating URLs and defaults.
# ---------------------------------------------------------------------------
# probes/__init__.py puts src/ on sys.path when the package is not installed.
from mockboard.config import Settings, get_settings

__all__ = [
    "FRAME_BYTES",
    "VAD_LIMITS",
    "STTConfig",
    "STTMessage",
    "Settings",
    "TTSChunk",
    "TTSRequest",
    "api_key",
    "build_stt_headers",
    "build_tts_headers",
    "frame_duration_s",
    "get_settings",
    "iter_frames",
    "pace_frames",
    "parse_sse_lines",
    "parse_stt_message",
    "parse_tts_ws_message",
    "update_settings_frame",
]

# ---------------------------------------------------------------------------
# STT (Prisma v2.5) WebSocket
# ---------------------------------------------------------------------------

# Docs: "Each binary frame is exactly 1,024 bytes (512 samples)". The `connected` message
# echoes `config.chunk_size` = 512 (samples).
FRAME_BYTES = 1024
SAMPLES_PER_FRAME = FRAME_BYTES // 2  # s16le mono => 2 bytes per sample

STT_SAMPLE_RATES = (8000, 16000, 44100, 48000)
STT_FORMATS = ("verbatim", "transcribe")
BIAS_SCORES = (0.0, 0.5, 1.0, 1.5, 2.0)
MAX_BIAS_WORDS = 100

# Documented VAD header ranges (all fixed per connection).
VAD_LIMITS: dict[str, tuple[float, float]] = {
    "x-vad-threshold": (0.0, 1.0),  # default 0.7
    "x-min-silence-ms": (100, 2000),  # default 500
    "x-min-speech-ms": (50, 2000),  # default 100 (200 at 8 kHz)
    "x-min-segment-ms": (100, 1000),  # default 120; too high silently drops short answers
    "x-max-speech-s": (5.0, 120.0),  # disabled by default
}


def frame_duration_s(sample_rate: int) -> float:
    """Real-time duration of one 1,024-byte frame: 32 ms at 16 kHz, 64 ms at 8 kHz."""
    return SAMPLES_PER_FRAME / sample_rate


@dataclass
class STTConfig:
    """Everything Prisma takes at connection time. Built from `Settings` by default."""

    lang_code: str = "en-IN"
    sample_rate: int = 16000
    fmt: str = "verbatim"
    max_speech_s: float | None = 15.0
    vad_threshold: float | None = None
    min_silence_ms: int | None = None
    min_speech_ms: int | None = None
    min_segment_ms: int | None = None
    partials: bool = False
    bias_list: list[str] = field(default_factory=list)
    bias_score: float | None = None

    @classmethod
    def from_settings(cls, s: Settings, **overrides: Any) -> STTConfig:
        base = cls(
            lang_code=s.stt_lang_code,
            sample_rate=s.stt_sample_rate,
            fmt=s.stt_format,
            max_speech_s=s.stt_max_speech_s,
            partials=s.feature_prisma_partials,
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(base, k, v)
        return base


def _check_range(name: str, value: float) -> None:
    lo, hi = VAD_LIMITS[name]
    if not lo <= value <= hi:
        raise ValueError(f"{name}={value} is outside the documented range {lo}-{hi}")


def validate_bias_words(words: list[str]) -> list[str]:
    """Docs: up to 100 single words, letters only. Invalid input is skipped silently by
    the server, so we reject it loudly here instead."""
    if len(words) > MAX_BIAS_WORDS:
        raise ValueError(f"bias list has {len(words)} words; the documented limit is 100")
    bad = [w for w in words if not w.isalpha()]
    if bad:
        raise ValueError(f"bias words must be single letters-only words: {bad}")
    # UNVERIFIED: whether Devanagari letters count as "letters only" (str.isalpha accepts
    # Devanagari consonants but not combining vowel signs such as U+093E).
    return words


def build_stt_headers(cfg: STTConfig, key: str) -> dict[str, str]:
    """WebSocket upgrade headers for wss://api.vachana.ai/stt/v3/stream.

    Header names are written exactly as in the docs: `lang_code` uses an underscore while
    the others use hyphens. All values are strings because they travel as HTTP headers.
    """
    if cfg.sample_rate not in STT_SAMPLE_RATES:
        raise ValueError(f"x-sample-rate must be one of {STT_SAMPLE_RATES}")
    if cfg.fmt not in STT_FORMATS:
        raise ValueError(f"x-format must be one of {STT_FORMATS}")
    h: dict[str, str] = {
        "x-api-key-id": key,
        "lang_code": cfg.lang_code,
        "x-sample-rate": str(cfg.sample_rate),
        "x-format": cfg.fmt,
    }
    optional = {
        "x-vad-threshold": cfg.vad_threshold,
        "x-min-silence-ms": cfg.min_silence_ms,
        "x-min-speech-ms": cfg.min_speech_ms,
        "x-min-segment-ms": cfg.min_segment_ms,
        "x-max-speech-s": cfg.max_speech_s,
    }
    for name, value in optional.items():
        if value is None:
            continue
        _check_range(name, float(value))
        h[name] = f"{value:g}" if isinstance(value, float) else str(value)
    if cfg.bias_list:
        validate_bias_words(cfg.bias_list)
        h["x-bias-list"] = json.dumps(cfg.bias_list, ensure_ascii=False)
        if cfg.bias_score is not None:
            h["x-bias-score"] = _bias_score_str(cfg.bias_score)
    if cfg.partials:
        # UNVERIFIED: `x-partials` appears once in the docs ("If `x-partials` is on, they
        # also apply to `partial_transcript` results") and is otherwise undocumented. The
        # value "true" is our guess; the probe exists to find out what the server does.
        h["x-partials"] = "true"
    # UNVERIFIED: the docs do not say whether header names are case-sensitive. HTTP header
    # names are case-insensitive by spec, so lower-case should be safe.
    return h


def _bias_score_str(score: float) -> str:
    if score not in BIAS_SCORES:
        raise ValueError(f"bias score must be one of {BIAS_SCORES}")
    return f"{score:.1f}"


def update_settings_frame(
    bias_list: list[str] | None = None,
    bias_score: float | None = None,
    substitution: dict[str, Any] | None = None,
) -> str:
    """JSON text frame that changes word boosting / substitution mid-session.

    Docs example: {"type": "update_settings", "bias": {"list": ["Vachana", "EMI"], "score": 1.5}}
    Omitted keys keep their current values; changes apply from the next speech segment;
    an invalid update is ignored as a whole. The server replies with `settings_updated`.
    """
    msg: dict[str, Any] = {"type": "update_settings"}
    if bias_list is not None or bias_score is not None:
        bias: dict[str, Any] = {}
        if bias_list is not None:
            bias["list"] = validate_bias_words(bias_list)
        if bias_score is not None:
            _bias_score_str(bias_score)
            bias["score"] = bias_score
        msg["bias"] = bias
    if substitution is not None:
        if len(substitution.get("map", [])) > 10:
            raise ValueError("substitution map is limited to 10 rules")
        msg["substitution"] = substitution
    return json.dumps(msg, ensure_ascii=False)


def iter_frames(pcm: bytes) -> Iterator[bytes]:
    """Split raw s16le PCM into exact 1,024-byte frames.

    The last partial frame is padded with zero bytes (digital silence) so that every frame
    is exactly 1,024 bytes, as the docs require.
    # UNVERIFIED: whether the server would accept a short final frame instead of padding.
    """
    for i in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[i : i + FRAME_BYTES]
        if len(chunk) < FRAME_BYTES:
            chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
        yield chunk


def pace_frames(n_frames: int, sample_rate: int, t0: float) -> Iterator[tuple[int, float]]:
    """Yield (frame_index, monotonic deadline) for real-time cadence.

    Deadlines are absolute (t0 + i * 32 ms), not cumulative sleeps, so scheduler jitter
    does not accumulate into drift over a long clip.
    """
    dt = frame_duration_s(sample_rate)
    for i in range(n_frames):
        yield i, t0 + i * dt


@dataclass
class STTMessage:
    """One parsed server message, stamped with our local arrival time."""

    type: str
    raw: dict[str, Any]
    arrived: float  # time.monotonic()

    @property
    def text(self) -> str:
        return str(self.raw.get("text", ""))

    @property
    def is_partial(self) -> bool:
        """True for anything that looks like an interim result.

        # UNVERIFIED: the docs name a `partial_transcript` result but never define it, and
        # define no `is_final` field. Pipecat's Gnani service treats `is_final: false` as
        # interim, so we accept either signal, plus any type containing "partial"/"interim".
        """
        t = self.type.lower()
        return (
            "partial" in t
            or "interim" in t
            or self.raw.get("is_final") is False
            or self.raw.get("is_partial") is True
        )


KNOWN_STT_TYPES = {"connected", "processing", "transcript", "error", "settings_updated"}


def parse_stt_message(data: str | bytes, arrived: float | None = None) -> STTMessage:
    """Parse a server text frame. Unknown or non-JSON frames are kept, never dropped."""
    arrived = time.monotonic() if arrived is None else arrived
    if isinstance(data, bytes):
        return STTMessage("binary", {"bytes": len(data)}, arrived)
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return STTMessage("non_json", {"text_frame": data}, arrived)
    if not isinstance(obj, dict):
        return STTMessage("non_object", {"value": obj}, arrived)
    return STTMessage(str(obj.get("type", "untyped")), obj, arrived)


# UNVERIFIED: the docs do not define an end-of-audio signal (no EOF message, no empty
# frame). The probe sends ~1.5 s of trailing silence so server VAD closes the last
# segment, waits for the final transcript, then closes the socket normally.
TRAILING_SILENCE_S = 1.5


# ---------------------------------------------------------------------------
# TTS (Timbre v2.5) WebSocket and SSE
# ---------------------------------------------------------------------------

TTS_MODELS = ("timbre-v2.5", "timbre-v3.0")
TTS_LANGUAGES = (
    "auto", "hi-IN", "en-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "bn-IN",
    "gu-IN", "hi-en",
)  # fmt: skip
TTS_SAMPLE_RATES = (8000, 16000, 22050, 24000, 44100, 48000)

# Voice catalog (docs.gnani.ai/api/TTS/available-voices.md, fetched Oct 25, 2026). Voices
# are not shared between models: a wrong pairing returns 400 listing the valid voices.
TIMBRE_V25_VOICES = {
    "Ambuja": "Hindi", "Asmita": "Tamil", "Bhavna": "Hindi", "Brinda": "Tamil",
    "Chitra": "Hindi", "Deepak": "Hindi", "Devika": "English", "Dhruva": "Bengali",
    "Falak": "Gujarati", "Girish": "English", "Hemraj": "Hindi", "Ishaan": "Marathi",
    "Jalaj": "Hindi", "Jwala": "Hindi", "Kaveri": "English", "Kavin": "Kannada",
    "Kirra": "Bengali", "Lavanya": "Telugu", "Lehara": "Telugu", "Mehuli": "Punjabi",
    "Nalini": "Hindi", "Noopur": "Tamil", "Omkar": "Hindi", "Poorvi": "Hinglish",
    "Pranav": "English", "Reshma": "Malayalam", "Riyaan": "Malayalam", "Roopesh": "Hindi",
    "Saanvi": "Kannada", "Shlok": "English", "Suhana": "Telugu", "Trisha": "Tamil",
    "Trupti": "English", "Urmila": "Hindi", "Varuni": "Telugu", "Vedika": "Tamil",
    "Veera": "Gujarati", "Vikrant": "Hindi", "Yashvi": "Hindi", "Yukti": "Telugu",
    "Zahira": "Marathi", "Zayan": "Punjabi",
}  # fmt: skip
TIMBRE_V30_VOICES = (
    "Chetana", "Kalpana", "Lakshmi", "Riyaa", "Shivani", "Tarika", "Vasudha",
    "Avinash", "Devansh", "Harshad", "Prakash", "Shubham", "Yashraj",
)  # fmt: skip


def check_voice(model: str, voice: str) -> None:
    """Fail before spending credits if the voice is not in that model's catalog.
    # UNVERIFIED: the docs do not say whether voice names are case-sensitive; we match
    # the catalog spelling exactly."""
    if model == "timbre-v2.5" and voice not in TIMBRE_V25_VOICES:
        raise ValueError(f"{voice!r} is not a Timbre v2.5 voice; see available-voices.md")
    if model == "timbre-v3.0" and voice not in TIMBRE_V30_VOICES:
        raise ValueError(f"{voice!r} is not a Timbre v3.0 voice; see available-voices.md")


@dataclass
class TTSRequest:
    """One synthesis request. The same JSON body is used for the WebSocket message and the
    SSE POST body (both docs list the same fields)."""

    text: str
    voice: str
    model: str = "timbre-v2.5"
    language: str = "hi-en"
    speed: float = 1.0
    sample_rate: int = 16000
    # Raw headerless 16-bit PCM, as PLAN_v2_DELTA §4 asks. The docs table maps
    # container=raw + linear_pcm to "Raw 16-bit PCM".
    # UNVERIFIED: the docs never show `raw` combined with `pcm_s16le`; we use the
    # documented `linear_pcm` + `raw` pair, which they describe as raw 16-bit PCM.
    encoding: str = "linear_pcm"
    container: str = "raw"

    def body(self) -> dict[str, Any]:
        if self.model not in TTS_MODELS:
            raise ValueError(f"model must be one of {TTS_MODELS}")
        if self.language not in TTS_LANGUAGES:
            raise ValueError(f"language must be one of {TTS_LANGUAGES}")
        if self.sample_rate not in TTS_SAMPLE_RATES:
            raise ValueError(f"sample_rate must be one of {TTS_SAMPLE_RATES}")
        if not 0.85 <= self.speed <= 1.15:
            raise ValueError("speed must be between 0.85 and 1.15")
        check_voice(self.model, self.voice)
        return {
            "text": self.text,
            "voice": self.voice,
            "model": self.model,
            "language": self.language,
            "speed": self.speed,
            "audio_config": {
                "sample_rate": self.sample_rate,
                "num_channels": 1,
                "sample_width": 2,
                "encoding": self.encoding,
                "container": self.container,
            },
        }

    def ws_message(self) -> str:
        return json.dumps(self.body(), ensure_ascii=False)


def build_tts_headers(key: str) -> dict[str, str]:
    """Headers for both the TTS WebSocket upgrade and the SSE POST. The docs list only
    `X-API-Key-ID` as required; their examples also send `Content-Type`."""
    return {"X-API-Key-ID": key, "Content-Type": "application/json"}


@dataclass
class TTSChunk:
    """One parsed TTS event (WebSocket message or SSE event)."""

    type: str  # start | audio | chunk | complete | error | other
    audio: bytes
    chunk_index: int | None
    is_final: bool
    raw: dict[str, Any]
    arrived: float

    @property
    def is_audio(self) -> bool:
        return bool(self.audio)

    @property
    def is_terminal(self) -> bool:
        return self.type in ("complete", "error") or self.is_final


def _chunk_from(kind: str, obj: dict[str, Any], arrived: float) -> TTSChunk:
    # WebSocket nests the payload under "data"; SSE puts it at the top of the data JSON.
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    b64 = data.get("audio") or ""
    return TTSChunk(
        type=kind,
        audio=base64.b64decode(b64) if b64 else b"",
        chunk_index=data.get("chunk_index"),
        is_final=bool(data.get("is_final", False)),
        raw=obj,
        arrived=arrived,
    )


def parse_tts_ws_message(data: str | bytes, arrived: float | None = None) -> TTSChunk:
    """Parse one TTS WebSocket message.

    Docs: audio arrives as base64 inside JSON, e.g.
      {"type": "audio", "data": {"chunk_index": 1, "audio": "<b64>", "is_final": false}}
    and ends with {"type": "complete", "data": {..., "audio": "", "is_final": true}}.
    """
    arrived = time.monotonic() if arrived is None else arrived
    if isinstance(data, bytes):
        # UNVERIFIED: the docs only describe base64-in-JSON; we accept binary frames as raw
        # audio in case the server ever sends them.
        return TTSChunk("audio", data, None, False, {"binary": len(data)}, arrived)
    obj = json.loads(data)
    return _chunk_from(str(obj.get("type", "other")), obj, arrived)


def parse_sse_lines(lines: Iterator[str], clock=time.monotonic) -> Iterator[TTSChunk]:
    """Parse a text/event-stream into TTSChunks.

    Docs format: an `event:` line naming the type (start | chunk | complete | error), then
    a `data:` line with JSON, then a blank line. Multi-line data is joined per the SSE spec.
    """
    event, data_lines = "message", []
    for line in lines:
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    obj = {"text": payload}
                if not isinstance(obj, dict):
                    obj = {"value": obj}
                yield _chunk_from(event, obj, clock())
            event, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue  # SSE comment / keep-alive
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data_lines.append(value)
    if data_lines:  # stream ended without a trailing blank line
        obj = json.loads("\n".join(data_lines))
        yield _chunk_from(event, obj, clock())


def strip_wav_header(audio: bytes) -> bytes:
    """If a chunk starts with a RIFF/WAVE header, return only the PCM `data` payload.

    # UNVERIFIED: with container=wav the docs do not say whether only the first chunk or
    # every chunk carries a header. We request container=raw to avoid the question, and
    # use this as a safety net.
    """
    if len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        i = 12
        while i + 8 <= len(audio):
            cid, size = audio[i : i + 4], int.from_bytes(audio[i + 4 : i + 8], "little")
            if cid == b"data":
                return audio[i + 8 : i + 8 + size]
            i += 8 + size + (size & 1)
    return audio


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def api_key(settings: Settings | None = None) -> str:
    """Read the Gnani key from settings (env var GNANI_API_KEY or .env). Never logged."""
    s = settings or get_settings()
    if s.gnani_api_key is None or not s.gnani_api_key.get_secret_value().strip():
        raise SystemExit("GNANI_API_KEY is not set. Put it in .env or export it first.")
    key = s.gnani_api_key.get_secret_value()
    if key.startswith("replace-me"):
        raise SystemExit("GNANI_API_KEY still has the placeholder value from .env.example.")
    return key


def redact_headers(h: dict[str, str]) -> dict[str, str]:
    """Copy of headers safe to print or save."""
    return {k: ("***" if "key" in k.lower() else v) for k, v in h.items()}
