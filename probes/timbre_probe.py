"""Timbre v2.5 TTS probe: first-byte latency and total time, WebSocket and SSE.

For each voice passed on the command line it:
1. opens a WebSocket, sends one synthesis request ("cold": includes the TLS/WebSocket
   handshake), then sends the same request again on the same socket ("warm"), and
2. sends the same request over SSE (the documented fallback).

It records connect time, time to the first audio byte, total time to `complete`, chunk
count and audio duration, saves each result as a 16-bit mono WAV under
probes/results/audio/, and appends a table to probes/results/RESULTS.md.

Voice names are never invented: they must be passed as arguments and are checked against
Gnani's published voice catalog (docs.gnani.ai/api/TTS/available-voices.md) before any
credits are spent. Each synthesis spends Timbre credits for the text length.
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

import httpx
import websockets
from websockets.asyncio.client import connect

from probes import gnani_ws as g
from probes.common import AUDIO_DIR, append_results, fmt_ms, write_wav

# A short Hinglish board line, Latin script, numbers written as words (the docs ask for
# spoken-form numbers). Override with --text.
DEFAULT_TEXT = (
    "Achha, aapne kaha ki tourism se rozgaar badhta hai. "
    "Kya aap ek example de sakte hain, apne zile se?"
)


@dataclass
class SynthResult:
    transport: str  # ws-cold | ws-warm | sse
    voice: str
    connect_ms: float | None = None
    first_byte_ms: float | None = None  # request sent -> first non-empty audio chunk
    total_ms: float | None = None  # request sent -> complete
    chunks: int = 0
    pcm: bytearray = field(default_factory=bytearray)
    events: list[str] = field(default_factory=list)
    error: str | None = None
    wav_path: Path | None = None

    def audio_ms(self, sample_rate: int) -> int:
        return int(len(self.pcm) / 2 / sample_rate * 1000)

    def row(self, sample_rate: int) -> dict[str, Any]:
        return {
            "voice": self.voice,
            "transport": self.transport,
            "connect_ms": fmt_ms(self.connect_ms),
            "first_byte_ms": fmt_ms(self.first_byte_ms),
            "total_ms": fmt_ms(self.total_ms),
            "chunks": self.chunks,
            "audio_ms": self.audio_ms(sample_rate),
            "events": ",".join(dict.fromkeys(self.events)),
            "wav": self.wav_path.name if self.wav_path else "",
            "error": self.error or "",
        }


def _proxy_for(url: str) -> bool | None:
    host = urlparse(url).hostname or ""
    return None if host in ("localhost", "127.0.0.1", "::1") else True


def _absorb(res: SynthResult, chunk: g.TTSChunk, t_req: float) -> None:
    res.events.append(chunk.type)
    if chunk.type == "error":
        res.error = str(chunk.raw.get("message") or chunk.raw)
    if chunk.is_audio:
        if res.first_byte_ms is None:
            res.first_byte_ms = (chunk.arrived - t_req) * 1000
        res.chunks += 1
        res.pcm += g.strip_wav_header(chunk.audio)


async def _recv_until_terminal(ws, res: SynthResult, t_req: float, timeout_s: float) -> None:
    while True:
        data = await asyncio.wait_for(ws.recv(), timeout_s)
        chunk = g.parse_tts_ws_message(data)
        _absorb(res, chunk, t_req)
        if chunk.is_terminal:
            res.total_ms = (chunk.arrived - t_req) * 1000
            return


async def run_ws(
    url: str,
    headers: dict[str, str],
    req: g.TTSRequest,
    warm_repeats: int = 1,
    timeout_s: float = 20.0,
) -> list[SynthResult]:
    """One cold request plus `warm_repeats` warm requests on the same WebSocket."""
    results: list[SynthResult] = []
    cold = SynthResult("ws-cold", req.voice)
    t_conn = time.monotonic()
    try:
        async with connect(
            url, additional_headers=headers, proxy=_proxy_for(url), max_size=None, open_timeout=15
        ) as ws:
            t_open = time.monotonic()
            cold.connect_ms = (t_open - t_conn) * 1000
            for i in range(1 + warm_repeats):
                res = cold if i == 0 else SynthResult("ws-warm", req.voice)
                results.append(res)
                # Cold first-byte is measured from connect start so it includes the handshake.
                t_req = t_conn if i == 0 else time.monotonic()
                await ws.send(req.ws_message())
                try:
                    await _recv_until_terminal(ws, res, t_req, timeout_s)
                except TimeoutError:
                    res.error = f"no complete within {timeout_s}s"
                    break
                if res.error:
                    break
    except websockets.exceptions.InvalidStatus as e:
        cold.error = f"handshake rejected: HTTP {e.response.status_code}"
    except websockets.exceptions.ConnectionClosed as e:
        (results[-1] if results else cold).error = f"closed: code={e.code} reason={e.reason!r}"
    except (OSError, TimeoutError) as e:
        cold.error = f"{type(e).__name__}: {e}"
    if not results:
        results.append(cold)
    return results


async def run_sse(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    req: g.TTSRequest,
    timeout_s: float = 30.0,
) -> SynthResult:
    """POST the request and read the event stream. First byte is measured from the POST."""
    res = SynthResult("sse", req.voice)
    t_req = time.monotonic()
    try:
        async with client.stream(
            "POST",
            url,
            headers={**headers, "Accept": "text/event-stream"},
            content=req.ws_message().encode("utf-8"),
            timeout=timeout_s,
        ) as resp:
            res.connect_ms = (time.monotonic() - t_req) * 1000  # time to response headers
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                res.error = f"HTTP {resp.status_code}: {body[:300]}"
                return res
            lines: list[str] = []
            # Feed lines to the shared SSE parser as they arrive, so timing is real.
            async for line in resp.aiter_lines():
                lines.append(line)
                if line == "":
                    for chunk in g.parse_sse_lines(iter(lines)):
                        _absorb(res, chunk, t_req)
                        if chunk.is_terminal:
                            res.total_ms = (chunk.arrived - t_req) * 1000
                    lines.clear()
            if lines:
                for chunk in g.parse_sse_lines(iter(lines)):
                    _absorb(res, chunk, t_req)
                    if chunk.is_terminal:
                        res.total_ms = (chunk.arrived - t_req) * 1000
    except httpx.HTTPError as e:
        res.error = f"{type(e).__name__}: {e}"
    return res


def save_audio(res: SynthResult, sample_rate: int, stamp: str) -> None:
    if res.pcm:
        res.wav_path = write_wav(
            AUDIO_DIR / f"timbre_{stamp}_{res.voice}_{res.transport}.wav",
            bytes(res.pcm),
            sample_rate,
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "voices",
        nargs="+",
        help="voice names from Gnani's catalog, e.g. the persona voice_candidates",
    )
    p.add_argument(
        "--text", default=DEFAULT_TEXT, help="line to synthesise (write numbers as words)"
    )
    p.add_argument("--language", default="hi-en", choices=g.TTS_LANGUAGES)
    p.add_argument(
        "--model", default=None, choices=g.TTS_MODELS, help="default from settings (timbre-v2.5)"
    )
    p.add_argument(
        "--sample-rate",
        type=int,
        default=None,
        choices=g.TTS_SAMPLE_RATES,
        help="default from settings (16000)",
    )
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument(
        "--warm-repeats", type=int, default=1, help="extra requests on the same WebSocket"
    )
    p.add_argument("--skip-ws", action="store_true")
    p.add_argument("--skip-sse", action="store_true")
    p.add_argument("--no-results", action="store_true", help="do not append to RESULTS.md")
    return p


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = g.get_settings()
    model = args.model or settings.tts_model
    rate = args.sample_rate or settings.tts_sample_rate
    reqs = []
    for v in args.voices:
        try:
            reqs.append(
                g.TTSRequest(
                    text=args.text,
                    voice=v,
                    model=model,
                    language=args.language,
                    speed=args.speed,
                    sample_rate=rate,
                )
            )
            reqs[-1].body()  # validates voice/model/rate before any network call
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    headers = g.build_tts_headers(g.api_key(settings))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    all_results: list[SynthResult] = []
    async with httpx.AsyncClient() as client:
        for req in reqs:
            if not args.skip_ws:
                all_results += await run_ws(
                    settings.gnani_tts_ws_url, headers, req, args.warm_repeats
                )
            if not args.skip_sse:
                all_results.append(await run_sse(client, settings.gnani_tts_sse_url, headers, req))
    rows = []
    for r in all_results:
        save_audio(r, rate, stamp)
        rows.append(r.row(rate))
        print(json.dumps(rows[-1], ensure_ascii=False))
    if not args.no_results:
        notes = [
            "ws-cold first_byte_ms includes the TLS + WebSocket handshake; "
            "ws-warm reuses the socket.",
            "sse connect_ms is the time to HTTP response headers.",
            "Listen to the WAVs in probes/results/audio/ before picking persona voices.",
        ]
        params = {
            "model": model,
            "language": args.language,
            "sample_rate": rate,
            "text_chars": len(args.text),
            "text": args.text,
        }
        print("appended to", append_results("Timbre TTS probe", rows, notes, params))
    return 0 if all(not r.error for r in all_results) else 1


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
