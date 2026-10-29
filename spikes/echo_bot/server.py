"""Local web server for the V1 echo spike: serves the page and the WebRTC offer endpoint.

Run from the repository root (see spikes/echo_bot/README.md):

    python -m spikes.echo_bot.server

then open http://localhost:7860 in Chrome. Browsers allow the microphone on `localhost`
without HTTPS (ruling 20), so no certificate is needed for a local run.

This is a small FastAPI app of our own rather than Pipecat's development runner
(`pipecat.runner.run.main`). Reasons, from reading 1.5.0: the runner calls
`load_dotenv(override=True)` at import time, mounts a prebuilt React client we do not need,
and finds the bot by scanning the current folder for any `bot` function. Here the offer
endpoint is the same few lines the runner uses (`SmallWebRTCRequestHandler`).
"""

import argparse
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# FastAPI reads these annotations at runtime, so they must be importable at module level.
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from mockboard.config import Settings

from .bot import EchoConfig, run_echo_bot

HERE = Path(__file__).resolve().parent


def _api_key(settings: Settings) -> str:
    """GNANI_API_KEY from the environment or .env (read by the app's own Settings)."""
    secret = settings.gnani_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if not key or key.startswith("replace-me"):
        sys.exit("GNANI_API_KEY is not set. Put it in .env (repository root) or export it first.")
    return key


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    s = Settings()
    p = argparse.ArgumentParser(description="Mock Board V1 echo spike (Pipecat 1.5.0 + Gnani)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--lang", default=s.stt_lang_code,
                   help="Prisma lang_code (default from STT_LANG_CODE, normally en-IN)")
    p.add_argument("--stt-format", default=s.stt_format, choices=["verbatim", "transcribe"])
    p.add_argument("--voice", default="Poorvi", help="Timbre v2.5 voice (Poorvi is Hinglish)")
    p.add_argument("--tts-language", default=s.tts_language,
                   help="Timbre language (default from TTS_LANGUAGE, normally auto); "
                        "e.g. hi-IN or en-IN; 'none' omits it")
    p.add_argument("--max-speech-s", type=float, default=s.stt_max_speech_s,
                   help="Prisma x-max-speech-s (default from STT_MAX_SPEECH_S)")
    p.add_argument("--min-silence-ms", type=int, default=None,
                   help="Prisma x-min-silence-ms (100-2000); default: Prisma's own (500)")
    p.add_argument("--vad-threshold", type=float, default=None,
                   help="Prisma x-vad-threshold (0-1); default: Prisma's own (0.7)")
    p.add_argument("--stock-stt", action="store_true",
                   help="the plugin's GnaniSTTService instead of ours (640-byte frames, no "
                        "VAD headers), for comparison")
    p.add_argument("--stock-tts", action="store_true",
                   help="the plugin's GnaniTTSService instead of ours (expect stale audio "
                        "after a barge-in)")
    p.add_argument("--vad-start-only", action="store_true",
                   help="only Silero may start a user turn (a late transcript cannot)")
    p.add_argument("--log-audio-frames", action="store_true",
                   help="log every audio frame, not only the first of each burst")
    p.add_argument("--log-dir", type=Path, default=HERE / "logs")
    p.add_argument("--fake-speech", action="store_true",
                   help="no Gnani: every pause gets a fake transcript and a beep back (free)")
    args = p.parse_args(argv)
    args.api_key = "" if args.fake_speech else _api_key(s)
    args.stt_url = s.gnani_stt_url
    return args


def create_app(cfg: EchoConfig) -> FastAPI:
    # Localhost only, so no STUN/TURN servers: host candidates are enough (V1b adds them).
    # UNVERIFIED: with Chrome on macOS (checked here only with a headless aiortc client).
    handler = SmallWebRTCRequestHandler()
    sessions: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for task in list(sessions):
            task.cancel()
        await handler.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(HERE / "index.html")

    @app.post("/api/offer")
    async def offer(request: Request):
        body = await request.json()
        webrtc_request = SmallWebRTCRequest.from_dict(
            {k: body.get(k) for k in ("sdp", "type", "pc_id", "restart_pc", "request_data")}
        )

        async def on_connection(connection):
            # One echo session per browser connection; keep a reference so it is not
            # garbage-collected while it runs.
            task = asyncio.create_task(run_echo_bot(connection, cfg))
            sessions.add(task)
            task.add_done_callback(sessions.discard)

        return await handler.handle_web_request(
            request=webrtc_request, webrtc_connection_callback=on_connection
        )

    @app.patch("/api/offer")
    async def ice_candidates(request: Request):
        # Only used if the page trickles ICE candidates; ours waits for gathering instead.
        body = await request.json()
        await handler.handle_patch_request(
            SmallWebRTCPatchRequest(
                pc_id=body["pc_id"],
                candidates=[IceCandidate(**c) for c in body.get("candidates", [])],
            )
        )
        return {"status": "success"}

    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = parse_args(argv)
    cfg = EchoConfig(
        api_key=args.api_key,
        stt_language=args.lang,
        stt_format=args.stt_format,
        tts_voice=args.voice,
        tts_language=None if args.tts_language.lower() == "none" else args.tts_language,
        stt_url=args.stt_url,
        stt_max_speech_s=args.max_speech_s,
        stt_min_silence_ms=args.min_silence_ms,
        stt_vad_threshold=args.vad_threshold,
        stock_stt=args.stock_stt,
        stock_tts=args.stock_tts,
        vad_start_only=args.vad_start_only,
        log_dir=args.log_dir,
        log_audio_frames=args.log_audio_frames,
        fake_speech=args.fake_speech,
    )
    print(f"\nEcho spike: open http://{args.host}:{args.port} in Chrome. Ctrl+C stops it.\n")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
