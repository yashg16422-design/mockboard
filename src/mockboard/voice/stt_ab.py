"""Replay one recording through Prisma with several settings and print the transcripts side by
side (ruling 35), so the owner picks the STT setting by reading his own Hinglish.

    python -m mockboard.voice.stt_ab                         # newest traces/*.mic.wav
    python -m mockboard.voice.stt_ab traces/voice-abc.mic.wav
    python -m mockboard.voice.stt_ab my.wav --only en hi     # a subset
    python -m mockboard.voice.stt_ab my.wav --dry-run        # headers and cost only, free

The three arms (ruling 35):

* `en`    prisma-v2.5, language_code=en-IN, format=verbatim (what the first real run used)
* `hi`    prisma-v2.5, language_code=hi-IN, format=verbatim
* `v3mix` prisma-v3.0, language_code=hi-IN,en-IN, format=codemix (CTO_NOTES_05 §4)

Header values checked against https://docs.gnani.ai/api/STT/stt-websocket.md on Oct 31, 2026:
`model` is `prisma-v2.5` | `prisma-v3.0`; `language_code` takes a comma-separated list of 1-4
languages on v3.0 only; `format` is `verbatim` (default) | `transcribe` | `codemix` (v3.0
only: English words stay in Latin script, Hindi in Devanagari) | `transliterate` (v3.0 only).
Each column shows its timing: our clock from Prisma's `processing` to the final, and the
server's own `latency` field.

Each arm streams the whole recording once, at real-time pace, in 1,024-byte frames, with the
app's own headers (`build_stt_headers`, the same VAD values as the voice app), so each arm
spends about as many Gnani credits as the recording is long (UNVERIFIED billing; see
VOICE_QUICKSTART). Arms run one after another, never two sockets at once. A run writes
`<recording>.stt_ab.md` next to the recording with the same table. The key is read from
GNANI_API_KEY and never printed. A recording is made with `server --record-audio`; any 16-bit
WAV works (other rates are resampled to 16 kHz if numpy and scipy are installed).
UNVERIFIED: everything here against the real Prisma (tested against a fake server only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from websockets.asyncio.client import connect as websocket_connect

from mockboard.config import Settings
from mockboard.speech.gnani_protocol import (
    COMMIT_FRAME,
    STTConfig,
    build_stt_headers,
    frame_duration_s,
    iter_frames,
    parse_stt_message,
    redact_headers,
)

SAMPLE_RATE = 16000


@dataclass(frozen=True)
class Arm:
    key: str
    label: str
    model: str
    language: str
    fmt: str


ARMS = {
    "en": Arm("en", "v2.5 en-IN", "prisma-v2.5", "en-IN", "verbatim"),
    "hi": Arm("hi", "v2.5 hi-IN", "prisma-v2.5", "hi-IN", "verbatim"),
    "v3mix": Arm("v3mix", "v3.0 hi-IN,en-IN codemix", "prisma-v3.0", "hi-IN,en-IN", "codemix"),
}


@dataclass
class Segment:
    start_s: float  # audio sent when the segment's `processing` arrived (approximate end)
    text: str
    latency_ms: Any = None  # the server's own `latency` field, as sent
    language: Any = None
    wait_ms: int | None = None  # our clock: `processing` -> `transcript`


@dataclass
class ArmResult:
    arm: Arm
    segments: list[Segment] = field(default_factory=list)
    connected: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    wall_s: float = 0.0  # connect to last message, our clock

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text).strip()

    def timing(self) -> str:
        """One line: how long each final took after its `processing`, and the server's own
        `latency` field, as medians and maxima (milliseconds)."""
        waits = [s.wait_ms for s in self.segments if isinstance(s.wait_ms, int)]
        lats = [s.latency_ms for s in self.segments if isinstance(s.latency_ms, int | float)]
        parts = [f"wall {self.wall_s:.1f} s"]
        if waits:
            parts.append(f"processing->final median {median(waits):.0f} ms, max {max(waits)} ms")
        if lats:
            parts.append(f"server latency median {median(lats):.0f} ms")
        return "; ".join(parts)


def load_pcm(path: Path) -> bytes:
    """16 kHz 16-bit mono PCM from a WAV (resampled and mixed down if needed)."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        data = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"{path}: need 16-bit PCM WAV (got {8 * width}-bit)")
    if rate == SAMPLE_RATE and channels == 1:
        return data
    try:
        import numpy as np
        from scipy.signal import resample_poly
    except ImportError:
        raise SystemExit(f"{path} is {rate} Hz / {channels} ch; convert it to 16 kHz mono "
                         "(or install numpy and scipy: requirements-probes.txt)") from None
    x = np.frombuffer(data, "<i2").astype(float).reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        x = resample_poly(x, SAMPLE_RATE, rate)
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def arm_config(settings: Settings, arm: Arm) -> STTConfig:
    return STTConfig.from_settings(settings, lang_code=arm.language, fmt=arm.fmt,
                                   model=arm.model)


async def run_arm(url: str, key: str, settings: Settings, arm: Arm, pcm: bytes, *,
                  tail_s: float = 1.0, settle_s: float = 4.0, connect=None,
                  pace: bool = True) -> ArmResult:
    """Stream `pcm` once with this arm's headers and collect the finals."""
    connect = connect or websocket_connect
    res = ArmResult(arm)
    headers = build_stt_headers(arm_config(settings, arm), key)
    headers["x-api-request-id"] = f"stt-ab-{arm.key}-{int(time.time())}"
    frames = list(iter_frames(pcm + b"\x00\x00" * int(SAMPLE_RATE * tail_s)))
    dt = frame_duration_s(SAMPLE_RATE)
    sent = 0
    opened: list[tuple[float, float]] = []  # (audio sent, our clock) per open `processing`
    last_msg = time.monotonic()
    t0 = time.monotonic()
    async with connect(url, additional_headers=headers, ping_interval=20, close_timeout=5) as ws:
        first = parse_stt_message(await asyncio.wait_for(ws.recv(), 10))
        res.connected = first.raw
        if first.type == "error":
            res.errors.append(str(first.raw.get("message")))
            return res

        async def receive():
            nonlocal last_msg
            async for data in ws:
                msg = parse_stt_message(data)
                now = last_msg = time.monotonic()
                if msg.type == "processing":
                    opened.append((sent * dt, now))
                elif msg.type == "transcript":
                    audio_s, t_proc = opened.pop(0) if opened else (sent * dt, None)
                    raw = msg.raw
                    wait = None if t_proc is None else round((now - t_proc) * 1000)
                    res.segments.append(Segment(audio_s, msg.text.strip(), raw.get("latency"),
                                                raw.get("detected_language_code"), wait))
                elif msg.type == "error":
                    res.errors.append(str(msg.raw.get("message")))

        rx = asyncio.create_task(receive())
        try:
            for i, frame in enumerate(frames):
                if pace:
                    delay = t0 + i * dt - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                await ws.send(frame)
                sent = i + 1
            await ws.send(COMMIT_FRAME)
            end = time.monotonic() + settle_s
            while time.monotonic() < end:
                if not opened and time.monotonic() - last_msg > 1.0:
                    break
                await asyncio.sleep(0.05)
        finally:
            rx.cancel()
    res.seconds = len(pcm) / 2 / SAMPLE_RATE
    res.wall_s = last_msg - t0
    return res


def render(results: list[ArmResult], recording: Path) -> str:
    lines = [f"# STT A/B: {recording.name}", "",
             f"Audio: {results[0].seconds:.1f} s per arm. Read each column and pick the one "
             "that best matches what you said.", ""]
    for r in results:
        conf = r.connected.get("config") if isinstance(r.connected.get("config"), dict) else {}
        served = (f"v3.0 fields present, language {conf.get('language_code')}, format "
                  f"{conf.get('format')}" if "request_id" in r.connected else "no v3.0 fields")
        lines.append(f"## {r.arm.label}  ({len(r.segments)} segments; connected: {served})")
        lines.append(f"timing: {r.timing()}")
        langs = sorted({str(s.language) for s in r.segments if s.language})
        if langs:
            lines.append(f"detected languages: {', '.join(langs)}")
        if r.errors:
            lines.append(f"errors: {'; '.join(r.errors)}")
        lines.append("")
        lines.append(r.text or "(no words)")
        lines.append("")
    # Segment table, aligned by when each segment ended in the audio (nearest second).
    lines += ["## By time (s)", "",
              "t is the audio second where each segment ended; (ms) is how long its final took "
              "after Prisma's `processing` message.", "",
              "| t | " + " | ".join(r.arm.label for r in results) + " |",
              "|---|" + "---|" * len(results)]
    times = sorted({round(s.start_s) for r in results for s in r.segments})
    for t in times:
        cells = []
        for r in results:
            segs = [s for s in r.segments if round(s.start_s) == t]
            cell = " ".join(s.text + (f" ({s.wait_ms} ms)" if s.wait_ms is not None else "")
                            for s in segs)
            cells.append(cell.replace("|", "/") or " ")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def newest_recording(trace_dir: Path) -> Path | None:
    found = sorted(trace_dir.glob("*.mic.wav"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


async def amain(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mockboard.voice.stt_ab")
    p.add_argument("recording", nargs="?", help="a WAV (default: newest traces/*.mic.wav)")
    p.add_argument("--only", nargs="+", choices=sorted(ARMS), help="arms to run")
    p.add_argument("--trace-dir", help="where to look for recordings (default TRACE_DIR)")
    p.add_argument("--dry-run", action="store_true", help="show headers and cost; no network")
    p.add_argument("--url", help="override GNANI_STT_URL (tests)")
    a = p.parse_args(argv)
    settings = Settings()
    trace_dir = Path(a.trace_dir) if a.trace_dir else Path(settings.trace_dir)
    path = Path(a.recording) if a.recording else newest_recording(trace_dir)
    if path is None or not path.exists():
        print("No recording. Run the voice app with --record-audio first, or pass a WAV.",
              file=sys.stderr)
        return 1
    pcm = load_pcm(path)
    arms = [ARMS[k] for k in (a.only or list(ARMS))]
    minutes = len(pcm) / 2 / SAMPLE_RATE / 60
    print(f"{path}: {minutes * 60:.1f} s of audio x {len(arms)} arms (about "
          f"{minutes * len(arms):.1f} audio-minutes of Prisma).")
    secret = settings.gnani_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if a.dry_run:
        for arm in arms:
            hdr = build_stt_headers(arm_config(settings, arm), key or "none")
            print(f"{arm.key:>6}: {json.dumps(redact_headers(hdr))}")
        return 0
    if not key or key.startswith("replace-me"):
        print("GNANI_API_KEY is not set (.env).", file=sys.stderr)
        return 1
    results = []
    for arm in arms:
        print(f"... {arm.label}", flush=True)
        try:
            results.append(await run_arm(a.url or settings.gnani_stt_url, key, settings, arm,
                                         pcm))
        except Exception as e:  # one arm failing (401, refused header) must not lose the rest
            failed = ArmResult(arm, seconds=len(pcm) / 2 / SAMPLE_RATE)
            failed.errors.append(f"{type(e).__name__}: {e}")
            results.append(failed)
    text = render(results, path)
    out = path.with_name(path.name.removesuffix(".wav") + ".stt_ab.md")
    out.write_text(text, encoding="utf-8")
    print()
    print(text)
    print(f"Saved: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
