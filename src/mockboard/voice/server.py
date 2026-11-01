"""The local voice app: talk to the real board in Chrome on http://localhost:7860.

Run from the repository root (see docs/VOICE_QUICKSTART.md):

    python -m mockboard.voice.server --mode upsc
    python -m mockboard.voice.server --mode debate --motion 1 --side for
    python -m mockboard.voice.server --mode upsc --fake-llm --fake-speech    # free

By default it uses real Gnani Prisma (STT) and Timbre (TTS) with GNANI_API_KEY, and real
Amazon Nova Pro in AWS_REGION (ap-southeast-2) through the AWS credential chain
(`AWS_PROFILE`). `--fake-llm` swaps in the offline rule-based model and `--fake-speech` the
fake speech services (beeps instead of a voice, scripted answers instead of your words).

Browsers allow the microphone on `localhost` without HTTPS (ruling 20), so no certificate is
needed. One browser connection is one session: the page sends the application form (UPSC;
no names or identifiers) with its WebRTC offer, the server builds a session and a pipeline
(`pipeline.py`), and the report is shown in the page and saved under `traces/`.

This is our own small FastAPI app, as in the V1 spike (spikes/echo_bot/server.py): Pipecat's
development runner calls `load_dotenv(override=True)` at import and scans for a bot function.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# FastAPI reads these annotations at runtime, so they are imported at module level.
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import ValidationError

from mockboard.brain.cards import load_cards_or_builtin
from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.schemas import CandidateForm
from mockboard.cli import DEFAULT_FAKE_FORM, _form_values, build_llm
from mockboard.config import Settings
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession
from mockboard.speech.keyterms import speech_context
from mockboard.voice.gate import PageSessions
from mockboard.voice.lines import PlaybackLedger

HERE = Path(__file__).resolve().parent
_STATIC = (HERE / "static").resolve()
MODES = {"upsc": "upsc_interview", "debate": "debate"}
FORM_FIELDS = ("home_state", "district", "degree_subject", "hobbies", "optional_subject",
               "interview_language")


@dataclass
class AppConfig:
    """One run of the server. Holds no secret itself: keys stay inside `settings`."""

    mode: str  # a mode id: upsc_interview | debate
    settings: Settings = field(repr=False)
    fake_llm: bool = False
    fake_speech: bool = False
    voice: str = "Poorvi"
    max_turns: int = 9
    motion: Any = None  # debate: the curated Motion
    side: str = "for"
    length: str = "quick"
    language: str = "hinglish"
    opponent: str = "logician"
    host: str = "localhost"
    port: int = 7860
    record_audio: bool = False  # ruling 35: opt-in, saved only under the trace folder

    def public(self) -> dict[str, Any]:
        """What the page may show (no keys, no paths)."""
        out = {
            "mode": "debate" if self.mode == "debate" else "upsc",
            "fake_llm": self.fake_llm,
            "fake_speech": self.fake_speech,
            "voice": self.voice,
            "tts_model": self.settings.tts_model,
            "stt_model": self.settings.stt_model,
            "llm": "fake" if self.fake_llm else self.settings.nova_model_id,
            "region": None if self.fake_llm else self.settings.aws_region,
            "max_minutes": round(self.settings.voice_session_max_s / 60, 1),
            "record_audio": self.record_audio,
        }
        if self.motion is not None:
            out.update(motion=self.motion.text, motion_number=self.motion.number, side=self.side,
                       length=self.length, language=self.language, opponent=self.opponent)
        return out


def _static_path(asset: str) -> Path | None:
    """A file that sits directly in voice/static. No subpaths, no lab page."""
    if not asset or asset == "lab.html" or asset.startswith(".") or "/" in asset or "\\" in asset:
        return None
    path = (_STATIC / asset).resolve()
    if path.parent != _STATIC or not path.is_file():
        return None
    return path


def parse_form(data: dict[str, Any] | None, *, allow_default: bool) -> CandidateForm:
    """The page's application form. Only the known fields are read; no identifiers exist."""
    raw = {k: v for k, v in (data or {}).items() if k in FORM_FIELDS and v not in (None, "")}
    if not raw and allow_default:
        raw = dict(DEFAULT_FAKE_FORM)
    return CandidateForm.model_validate(_form_values(raw))


def make_session(cfg: AppConfig, form: CandidateForm | None) -> InterviewSession:
    """The same session the text CLI builds (cli.run), with a fresh trace file."""
    from mockboard.modes.debate.motions import Side
    from mockboard.modes.debate.session import DebateSetup, Length, OpponentStyle

    s = cfg.settings
    cards, _origin = load_cards_or_builtin(s.cards_dir, s.topics)
    session_id = f"voice-{uuid.uuid4().hex[:10]}"
    recorder = TraceRecorder(s.trace_dir, session_id)
    llm = build_llm(s, cfg.fake_llm, cards, recorder)
    deps = build_deps(s, llm, cards=cards)
    options = None
    if cfg.mode == "debate":
        options = DebateSetup(motion=cfg.motion, user_side=Side(cfg.side),
                              length=Length(cfg.length), language=cfg.language,
                              opponent_style=OpponentStyle(cfg.opponent))
        form = None
    return InterviewSession(deps=deps, form=form, recorder=recorder, analyzer=TurnAnalyzer(deps),
                            session_id=session_id, max_turns=cfg.max_turns, mode=cfg.mode,
                            mode_options=options)


def make_speech(cfg: AppConfig, session: InterviewSession, ledger: PlaybackLedger):
    """(stt, tts): ours on Gnani by default, or the fakes with --fake-speech."""
    if cfg.fake_speech:
        from mockboard.voice.fakes import FakeCommitSTT, FakeTTSService

        return FakeCommitSTT(), FakeTTSService(ledger=ledger, audible=True)
    from mockboard.speech.prisma_stt import PrismaSTTService
    from mockboard.speech.timbre_tts import TimbreTTSService

    s = cfg.settings
    keyterms, topic = speech_context(session) if s.stt_context_enabled else ([], None)
    # Ruling 31: x-api-request-id = <session id>-c<k>, so Gnani's ids match our trace.
    # UNVERIFIED: against the real Prisma and Timbre services (fake servers only so far).
    stt = PrismaSTTService.from_settings(
        s, keyterms=keyterms or None, topic=topic,
        service_kwargs={"request_id_prefix": session.session_id},
    )
    tts = TimbreTTSService.from_settings(s, voice=cfg.voice, sample_rate=s.tts_sample_rate,
                                         ledger=ledger)
    return stt, tts


async def run_session(webrtc_connection, cfg: AppConfig, form: CandidateForm | None) -> None:
    """One browser connection, until it disconnects or the session ends."""
    from pipecat.transports.base_transport import TransportParams
    from pipecat.workers.runner import WorkerRunner

    from mockboard.voice.cv2_shim import ensure_cv2
    from mockboard.voice.pipeline import build_voice_pipeline

    ensure_cv2()  # before the transport's module-level `import cv2` (defect E)
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    session = make_session(cfg, form)
    ledger = PlaybackLedger()
    stt, tts = make_speech(cfg, session, ledger)
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    last_terms: list[list[str]] = [[]]

    async def update_context() -> None:  # per-topic keyterms (Prisma v3.0 only)
        if cfg.fake_speech or not cfg.settings.stt_context_enabled:
            return
        terms, _topic = speech_context(session)
        if terms != last_terms[0] and await stt.set_keyterms(terms):
            last_terms[0] = terms

    mic = None
    if cfg.record_audio:
        from mockboard.voice.recorder import MicRecorder

        mic = MicRecorder(cfg.settings.trace_dir / f"{session.session_id}.mic.wav")
    vp = build_voice_pipeline(
        session=session, settings=cfg.settings, transport_input=transport.input(),
        transport_output=transport.output(), stt=stt, tts=tts, ledger=ledger,
        report_dir=cfg.settings.trace_dir, on_listen=update_context, autostart=False,
        mic_recorder=mic,
    )

    @stt.event_handler("on_gave_up")
    async def _gave_up(_stt, message):
        await vp.brain.speech_lost(message)

    @tts.event_handler("on_first_byte_timeout")
    async def _no_audio(_tts, text):
        await vp.brain.tts_failed(text)

    @transport.event_handler("on_client_connected")
    async def _connected(_transport, _client):
        # The data channel opens around now; a short grace keeps the opener's captions.
        await vp.brain.begin(delay_s=0.5)

    @transport.event_handler("on_app_message")
    async def _page_message(_transport, message, _sender):
        if isinstance(message, dict) and message.get("type") == "mockboard" and (
                message.get("event") == "stop"):
            await vp.brain.request_stop()  # defect F: Stop gives a report

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_transport, _client):
        try:  # defect F: a closed tab still saves the report if there was an answer
            await vp.brain.finish_without_page()
        except Exception as e:
            logger.warning(f"[voice] report after disconnect failed: {type(e).__name__}: {e}")
        await vp.worker.cancel()

    logger.info(f"[voice] session {session.session_id} ({cfg.mode}) started")
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(vp.worker)
    try:
        await runner.run()
    finally:
        if mic is not None:
            mic.close()
            logger.info(f"[voice] your audio ({mic.seconds:.0f} s) was saved to {mic.path}")
        _write_session_log(cfg, session, vp, stt, tts, mic)
        logger.info(f"[voice] session {session.session_id} ended; trace {session.recorder.path}")


def _write_session_log(cfg: AppConfig, session, vp, stt, tts, mic=None) -> None:
    """Session-level facts for `mockboard.voice.summarize` (no transcript text, no keys)."""
    stats = {
        "stt_model_requested": cfg.settings.stt_model,
        "stt_model_served": getattr(stt, "served_model", None),
        "stt_stats": dict(getattr(stt, "stats", {}) or {}),
        "stt_gave_up": bool(getattr(stt, "gave_up", None)),
        "tts_voice": cfg.voice,
        "tts_model": cfg.settings.tts_model,
        "tts_language": cfg.settings.tts_language,
        "tts_sample_rate": cfg.settings.tts_sample_rate,
        "tts_request_unit": cfg.settings.tts_request_unit,
        "tts": {k: getattr(tts, k, None) for k in (
            "interruptions", "audio_dropped_after_interruption", "idle_reconnects",
            "serialise_waits", "serialise_timeouts", "first_byte_resends",
            "first_byte_failures")},
        "transcripts_dropped": len(vp.brain.dropped),
        "commits": vp.brain.commits,
        "ignored_short_speech_ms": list(getattr(vp.start_strategy, "ignored", [])),
        "turns": len(vp.brain.outcomes),
        "fake_llm": cfg.fake_llm,
        "fake_speech": cfg.fake_speech,
        "report_written": vp.brain.report is not None,
        "mic_recording": str(mic.path) if mic is not None and mic.frames_written else None,
        "events": [{k: v for k, v in e.items() if k != "text" and k != "heard"}
                   for e in vp.brain.events if e["event"] != "caption"],
    }
    try:
        session.recorder.write_event("voice_session", tag=session.session_id, **stats)
    except Exception as e:  # never lose the run over a log line
        logger.warning(f"[voice] could not write the session log: {e}")


def create_app(cfg: AppConfig) -> FastAPI:
    # Localhost only, so no STUN/TURN servers: host candidates are enough (V1b adds them).
    handler = SmallWebRTCRequestHandler()
    pending_forms: dict[str, CandidateForm | None] = {}

    async def start_session(connection) -> None:  # only for connections that connected
        await run_session(connection, cfg, pending_forms.pop(connection.pc_id, None))

    gate = PageSessions(start_session, connect_timeout_s=cfg.settings.voice_connect_timeout_s)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await gate.close_all()
        await handler.close()

    app = FastAPI(lifespan=lifespan)
    app.state.gate = gate  # for tests

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)  # no icon; avoids a 404 line in the log

    @app.get("/")
    async def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/static/{asset:path}")
    async def static_files(asset: str):
        # access: static-file mounting (css and js for the page; lab.html stays on the lab app)
        path = _static_path(asset)
        if path is None:
            return Response(status_code=404)
        return FileResponse(path)

    @app.get("/api/config")
    async def config():
        return cfg.public()

    @app.post("/api/offer")
    async def offer(request: Request):
        body = await request.json()
        data = body.get("request_data") or body.get("requestData") or {}
        form = None
        if cfg.mode != "debate":
            try:
                form = parse_form(data.get("form"), allow_default=cfg.fake_llm)
            except ValidationError as e:
                problems = [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
                return JSONResponse({"error": "form", "problems": problems}, status_code=400)
        webrtc_request = SmallWebRTCRequest.from_dict(
            {k: body.get(k) for k in ("sdp", "type", "pc_id", "restart_pc")}
        )

        page_id = str(data.get("page_id") or "")[:64] or None

        async def on_connection(connection):
            # Defect D: no pipeline (and no Gnani connection) until this connection connects;
            # a newer offer from the same page replaces it.
            pending_forms[connection.pc_id] = form
            await gate.offer(connection, page_id)

        return await handler.handle_web_request(
            request=webrtc_request, webrtc_connection_callback=on_connection
        )

    @app.patch("/api/offer")
    async def ice_candidates(request: Request):
        body = await request.json()
        await handler.handle_patch_request(
            SmallWebRTCPatchRequest(
                pc_id=body["pc_id"],
                candidates=[IceCandidate(**c) for c in body.get("candidates", [])],
            )
        )
        return {"status": "success"}

    return app


def _check_keys(cfg: AppConfig) -> None:
    s = cfg.settings
    if not cfg.fake_speech:
        secret = s.gnani_api_key
        key = secret.get_secret_value().strip() if secret is not None else ""
        if not key or key.startswith("replace-me"):
            sys.exit("GNANI_API_KEY is not set. Put it in .env (repository root), or run with "
                     "--fake-speech.")
    if not cfg.fake_llm:
        try:
            import boto3

            creds = boto3.Session().get_credentials()
        except Exception as e:
            sys.exit(f"AWS credentials could not be loaded ({type(e).__name__}: {e}). Check "
                     "AWS_PROFILE / `aws configure`, or run with --fake-llm.")
        if creds is None:
            sys.exit("No AWS credentials found. Set AWS_PROFILE (or run `aws configure`), or "
                     "run with --fake-llm.")


def parse_args(argv: list[str] | None = None) -> AppConfig:
    p = argparse.ArgumentParser(prog="python -m mockboard.voice.server",
                                description="Mock Board local voice app (localhost only).")
    p.add_argument("--mode", choices=sorted(MODES), default="upsc")
    p.add_argument("--motion", help="debate: curated motion number (1-13) or id")
    p.add_argument("--side", choices=["for", "against"], help="debate: your side")
    p.add_argument("--length", choices=["quick", "standard"], default="quick")
    p.add_argument("--language", choices=["hinglish", "hindi", "english"], default="hinglish",
                   help="debate language")
    p.add_argument("--opponent", choices=["logician", "wit"], default="logician")
    p.add_argument("--fake-llm", action="store_true", help="offline rule-based model (free)")
    p.add_argument("--fake-speech", action="store_true",
                   help="fake STT/TTS: beeps and scripted answers, no Gnani credits")
    p.add_argument("--voice", help="Timbre voice (default TTS_VOICE, normally Poorvi)")
    p.add_argument("--max-turns", type=int, default=9)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--trace-dir", help="override TRACE_DIR (default traces)")
    p.add_argument("--record-audio", action="store_true",
                   help="save YOUR microphone audio to traces/<session>.mic.wav on this computer "
                        "(off by default; for `python -m mockboard.voice.stt_ab`)")
    a = p.parse_args(argv)
    settings = Settings()
    if a.trace_dir:
        settings = settings.model_copy(update={"trace_dir": Path(a.trace_dir)})
    cfg = AppConfig(mode=MODES[a.mode], settings=settings, fake_llm=a.fake_llm,
                    fake_speech=a.fake_speech, voice=a.voice or settings.tts_voice,
                    max_turns=a.max_turns, length=a.length, language=a.language,
                    opponent=a.opponent, host=a.host, port=a.port,
                    record_audio=a.record_audio)
    if cfg.mode == "debate":
        from mockboard.modes.debate.motions import find_motion, load_curated_motions

        if not a.motion or not a.side:
            p.error("--mode debate needs --motion <1-13> and --side for|against")
        try:
            cfg.motion = find_motion(load_curated_motions(), a.motion)
        except KeyError as e:
            p.error(str(e))
        cfg.side = a.side
    return cfg


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    cfg = parse_args(argv)
    _check_keys(cfg)
    host, port = cfg.host, cfg.port
    shown = json.dumps(cfg.public(), ensure_ascii=False)
    print(f"\nMock Board voice app: open http://{host}:{port} in Chrome. Ctrl+C stops it.")
    print(f"Config: {shown}\nTraces and reports: {cfg.settings.trace_dir}/\n")
    uvicorn.run(create_app(cfg), host=host, port=port)


if __name__ == "__main__":
    main()
