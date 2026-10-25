"""Prisma v2.5 STT probe: stream a WAV at real-time cadence and measure what comes back.

What it reports, per session:
* every server message with its arrival time (console + a JSONL log under probes/results/)
* transcript latency three ways: the server's own `latency` field, our `processing` ->
  `transcript` gap, and "last audio frame sent -> final transcript" (what a user feels)
* segment count, whether any partial/interim message appeared, the raw concatenated text
  (verbatim mode keeps fillers, so the owner can check them by eye)
* sum of `audio_duration_ms` versus speech measured locally with an energy gate
* a mid-session `update_settings` bias frame and whether `settings_updated` came back

By default it runs two sessions, without and with the `x-partials: true` header, so the
two can be compared. Each session spends Prisma credits for the clip length.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.asyncio.client import connect

from probes import gnani_ws as g
from probes.common import (
    RESULTS_DIR,
    WavFormatError,
    append_results,
    fmt_ms,
    log_line,
    read_wav_16k_mono,
    speech_ms,
)


@dataclass
class SessionResult:
    partials_header: bool
    messages: list[g.STTMessage] = field(default_factory=list)
    t0: float = 0.0
    last_audio_sent: float | None = None
    update_sent: float | None = None
    error: str | None = None

    # -- derived metrics ----------------------------------------------------
    def of_type(self, t: str) -> list[g.STTMessage]:
        return [m for m in self.messages if m.type == t]

    @property
    def transcripts(self) -> list[g.STTMessage]:
        return [m for m in self.of_type("transcript") if not m.is_partial]

    @property
    def partials(self) -> list[g.STTMessage]:
        return [m for m in self.messages if m.is_partial]

    @property
    def raw_text(self) -> str:
        return " ".join(m.text for m in self.transcripts).strip()

    @property
    def server_latencies(self) -> list[float]:
        return [float(m.raw["latency"]) for m in self.transcripts if "latency" in m.raw]

    def processing_to_transcript_ms(self) -> list[float]:
        """Pair each transcript with the most recent `processing` before it."""
        out, last_proc = [], None
        for m in self.messages:
            if m.type == "processing":
                last_proc = m.arrived
            elif m.type == "transcript" and not m.is_partial and last_proc is not None:
                out.append((m.arrived - last_proc) * 1000)
                last_proc = None
        return out

    def last_audio_to_final_ms(self) -> float | None:
        if self.last_audio_sent is None or not self.transcripts:
            return None
        return (self.transcripts[-1].arrived - self.last_audio_sent) * 1000

    def settings_updated_ms(self) -> float | None:
        su = self.of_type("settings_updated")
        if not su or self.update_sent is None:
            return None
        return (su[0].arrived - self.update_sent) * 1000

    def audio_duration_ms_total(self) -> int:
        return sum(int(m.raw.get("audio_duration_ms") or 0) for m in self.transcripts)


async def run_session(
    url: str,
    headers: dict[str, str],
    pcm: bytes,
    sample_rate: int,
    *,
    update_frame: str | None,
    update_at_s: float,
    trailing_silence_s: float = g.TRAILING_SILENCE_S,
    idle_timeout_s: float = 6.0,
    partials_header: bool = False,
    quiet: bool = False,
) -> SessionResult:
    """Open one Prisma connection, stream `pcm` at real time, collect every message."""
    res = SessionResult(partials_header=partials_header)
    silence = b"\x00" * (int(trailing_silence_s * sample_rate) * 2)
    speech_frames = list(g.iter_frames(pcm))
    frames = speech_frames + list(g.iter_frames(silence))
    host = urlparse(url).hostname or ""
    proxy = None if host in ("localhost", "127.0.0.1", "::1") else True

    def show(t: float, kind: str, payload: Any) -> None:
        if not quiet:
            print(log_line(res.t0, t, kind, payload), flush=True)

    res.t0 = time.monotonic()
    try:
        async with connect(
            url, additional_headers=headers, proxy=proxy, max_size=None, open_timeout=15
        ) as ws:
            # Wait for `connected` before sending audio (docs: step 2 of the flow).
            first = g.parse_stt_message(await asyncio.wait_for(ws.recv(), 15))
            res.messages.append(first)
            show(first.arrived, first.type, first.raw)
            if first.type == "error":
                res.error = first.raw.get("message", "error before audio")
                return res

            sent_done = asyncio.Event()

            async def sender() -> None:
                t_start = time.monotonic()
                update_due = t_start + update_at_s if update_frame else None
                for i, deadline in g.pace_frames(len(frames), sample_rate, t_start):
                    delay = deadline - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if update_due is not None and time.monotonic() >= update_due:
                        await ws.send(update_frame)
                        res.update_sent = time.monotonic()
                        show(res.update_sent, "-> update_settings", update_frame)
                        update_due = None
                    await ws.send(frames[i])
                    if i == len(speech_frames) - 1:
                        res.last_audio_sent = time.monotonic()
                        show(res.last_audio_sent, "-> last wav frame", f"frame {i}")
                sent_done.set()

            async def receiver() -> None:
                # Poll in short slices so the idle clock starts as soon as sending ends,
                # even if a recv() was already waiting at that moment. Cancelling recv()
                # is safe in websockets' asyncio client (no message is lost).
                last = time.monotonic()
                while True:
                    try:
                        data = await asyncio.wait_for(ws.recv(), 0.25)
                    except TimeoutError:
                        if sent_done.is_set():
                            quiet_since = max(last, res.last_audio_sent or last)
                            if time.monotonic() - quiet_since >= idle_timeout_s:
                                return  # server quiet for idle_timeout_s after all audio
                        continue
                    m = g.parse_stt_message(data)
                    last = m.arrived
                    res.messages.append(m)
                    show(m.arrived, ("partial:" if m.is_partial else "") + m.type, m.raw)

            send_task = asyncio.create_task(sender())
            recv_task = asyncio.create_task(receiver())
            await send_task
            await recv_task
    except websockets.exceptions.InvalidStatus as e:
        res.error = f"handshake rejected: HTTP {e.response.status_code}"
    except websockets.exceptions.ConnectionClosed as e:
        res.error = f"connection closed: code={e.code} reason={e.reason!r}"
    except (OSError, TimeoutError) as e:
        res.error = f"{type(e).__name__}: {e}"
    return res


def summarise(res: SessionResult, measured_speech_ms: int, clip_ms: int) -> dict[str, Any]:
    p2t = res.processing_to_transcript_ms()
    srv = res.server_latencies
    return {
        "x-partials": "true" if res.partials_header else "off",
        "segments": len(res.transcripts),
        "partials_seen": len(res.partials),
        "server_latency_ms (mean)": fmt_ms(sum(srv) / len(srv)) if srv else "n/a",
        "processing->transcript ms (mean)": fmt_ms(sum(p2t) / len(p2t)) if p2t else "n/a",
        "last_audio->final ms": fmt_ms(res.last_audio_to_final_ms()),
        "audio_duration_ms sum": res.audio_duration_ms_total(),
        "measured_speech_ms": measured_speech_ms,
        "clip_ms": clip_ms,
        "settings_updated ms": fmt_ms(res.settings_updated_ms()),
        "message types": ",".join(sorted({m.type for m in res.messages})),
        "error": res.error or "",
    }


def save_jsonl(res: SessionResult, tag: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"prisma_{time.strftime('%Y%m%d_%H%M%S')}_{tag}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for m in res.messages:
            f.write(
                json.dumps(
                    {"t_ms": round((m.arrived - res.t0) * 1000, 1), "type": m.type, "msg": m.raw},
                    ensure_ascii=False,
                )
                + "\n"
            )
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("wav", type=Path, help="16 kHz mono 16-bit WAV file")
    p.add_argument(
        "--resample", action="store_true", help="convert other rates/stereo instead of rejecting"
    )
    p.add_argument("--lang", default=None, help="lang_code header (default from settings, en-IN)")
    p.add_argument(
        "--format",
        dest="fmt",
        choices=g.STT_FORMATS,
        default=None,
        help="x-format (default verbatim)",
    )
    p.add_argument("--vad-threshold", type=float, default=None)
    p.add_argument("--min-silence-ms", type=int, default=None)
    p.add_argument("--min-speech-ms", type=int, default=None)
    p.add_argument("--min-segment-ms", type=int, default=None)
    p.add_argument("--max-speech-s", type=float, default=None, help="default from settings (15)")
    p.add_argument(
        "--partials",
        choices=["off", "on", "both"],
        default="both",
        help="run without / with x-partials: true (both = two sessions)",
    )
    p.add_argument(
        "--bias-words",
        default="UPSC,Naxalism,tourism",
        help="comma-separated letters-only words for the mid-session update_settings frame "
        "('' to skip)",
    )
    p.add_argument("--bias-score", type=float, default=1.5, choices=g.BIAS_SCORES)
    p.add_argument(
        "--update-at-s",
        type=float,
        default=None,
        help="when to send update_settings (default: half the clip)",
    )
    p.add_argument(
        "--idle-timeout-s",
        type=float,
        default=6.0,
        help="stop after this much silence from the server once audio is sent",
    )
    p.add_argument("--url", default=None, help="override the STT URL (default from settings)")
    p.add_argument("--no-results", action="store_true", help="do not append to RESULTS.md")
    return p


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = g.get_settings()
    try:
        pcm, rate = read_wav_16k_mono(args.wav, resample=args.resample)
    except (WavFormatError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    clip_ms = int(len(pcm) / 2 / rate * 1000)
    measured = speech_ms(pcm, rate)
    key = g.api_key(settings)
    url = args.url or settings.gnani_stt_url

    words = [w.strip() for w in args.bias_words.split(",") if w.strip()]
    update = g.update_settings_frame(bias_list=words, bias_score=args.bias_score) if words else None
    update_at = args.update_at_s if args.update_at_s is not None else clip_ms / 2000

    modes = {"off": [False], "on": [True], "both": [False, True]}[args.partials]
    rows, texts = [], []
    for partials in modes:
        cfg = g.STTConfig.from_settings(
            settings,
            lang_code=args.lang,
            fmt=args.fmt,
            max_speech_s=args.max_speech_s,
            vad_threshold=args.vad_threshold,
            min_silence_ms=args.min_silence_ms,
            min_speech_ms=args.min_speech_ms,
            min_segment_ms=args.min_segment_ms,
        )
        cfg.partials = partials
        headers = g.build_stt_headers(cfg, key)
        print(f"\n=== session: x-partials={'true' if partials else 'off'} ===")
        print("headers:", g.redact_headers(headers))
        res = await run_session(
            url,
            headers,
            pcm,
            rate,
            update_frame=update,
            update_at_s=update_at,
            idle_timeout_s=args.idle_timeout_s,
            partials_header=partials,
        )
        log_path = save_jsonl(res, "partials" if partials else "nopartials")
        row = summarise(res, measured, clip_ms)
        rows.append(row)
        texts.append(f"x-partials={row['x-partials']}: {res.raw_text!r}")
        print("\nsummary:", json.dumps(row, ensure_ascii=False, indent=2))
        print("raw text:", res.raw_text)
        print("message log:", log_path)

    if not args.no_results:
        cfg_params = {
            "wav": args.wav.name,
            "lang": args.lang or settings.stt_lang_code,
            "format": args.fmt or settings.stt_format,
            "max_speech_s": args.max_speech_s or settings.stt_max_speech_s,
            "vad_threshold": args.vad_threshold,
            "min_silence_ms": args.min_silence_ms,
            "min_speech_ms": args.min_speech_ms,
            "min_segment_ms": args.min_segment_ms,
            "bias_words": words,
            "bias_score": args.bias_score,
        }
        notes = [f"Raw text ({t})" for t in texts]
        notes.append(
            "measured_speech_ms is a rough energy gate, not a VAD; use it only as a sanity check."
        )
        print("appended to", append_results("Prisma STT probe", rows, notes, cfg_params))
    return 0 if all(not r["error"] for r in rows) else 1


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
