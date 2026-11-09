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
import asyncio
import contextlib
import json
import sys
import time
import uuid
from collections import Counter  # access: in-flight offers
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime  # access: admission time
from pathlib import Path
from typing import Any

# FastAPI reads these annotations at runtime, so they are imported at module level.
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import ValidationError

from mockboard.access import (  # access: the limits
    COOKIE,
    AccessConfig,
    Identity,
    Limits,
    hash_ip,
    new_visitor_id,
    read_token,
    sign_token,
)
from mockboard.brain.cards import load_cards_or_builtin
from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.schemas import CandidateForm
from mockboard.cli import DEFAULT_FAKE_FORM, _form_values, build_llm
from mockboard.config import Settings
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession
from mockboard.speech.keyterms import speech_context
from mockboard.speech.mic_check import lang_code_for
from mockboard.speech.tts_text import hinglish_for_tts
from mockboard.voice.gate import PageSessions
from mockboard.voice.lines import PlaybackLedger

HERE = Path(__file__).resolve().parent
_STATIC = (HERE / "static").resolve()  # access: the page's own files
_FONT_FILES = (  # served from here, so the page calls no font service (Nov 7)
    "geist-latin.woff2", "geist-latin-ext.woff2", "geist-mono-latin.woff2",
    "geist-mono-latin-ext.woff2", "noto-sans-devanagari.woff2", "noto-sans-tamil.woff2",
)
_STATIC_FILES = frozenset(  # access: an exact allowlist, fonts included by name
    {"app.js", "presence.js", "site.css", "tokens.css", "fonts.css", "guide.css"}
    | {f"fonts/{name}" for name in _FONT_FILES}
)
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})  # access:
MAX_OFFERS_PER_IP = 4  # access: in-flight offers per IP hash; each holds a peer connection
MODES = {"upsc": "upsc_interview", "debate": "debate"}
FORM_FIELDS = ("home_state", "district", "degree_subject", "hobbies", "optional_subject",
               "work_experience", "interview_language")


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
    # P8: persona id -> Timbre voice for the panel. Empty: one voice (`voice`) for everyone, as
    # for debate and for an explicit --voice. Set by `parse_args` from `Settings.panel_voices()`.
    panel_voices: dict[str, str] = field(default_factory=dict)

    def voice_for(self):
        """The voice picker the voice loop uses, or None for a single voice (P8). A member the
        table leaves out speaks in `voice`."""
        voices = dict(self.panel_voices)
        if not voices:
            return None
        return lambda member: voices.get(member) or self.voice

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
            "max_minutes": round(self.settings.time_limits(self.mode)[1] / 60, 1),
            "record_audio": self.record_audio,
        }
        if self.motion is not None:
            out.update(motion=self.motion.text, motion_number=self.motion.number, side=self.side,
                       length=self.length, language=self.language, opponent=self.opponent)
        out["modes"] = available_modes()
        out["minutes_by_mode"] = {
            m: round(self.settings.time_limits(SITTING_MODES[m])[1] / 60, 1) for m in out["modes"]
        }
        out["motions"] = curated_motion_list()
        return out


def available_modes() -> list[str]:
    """The modes this server can run, in the picker's order. The study partner shows only when
    its package is installed (it is new on Nov 7)."""
    modes = ["upsc", "debate"]
    try:
        import mockboard.modes.partner.session  # noqa: F401
    except ImportError:
        return modes
    return [*modes, "partner"]


def curated_motion_list() -> list[dict[str, Any]]:
    """The debate picker's choices: number, short title and the motion, nothing else."""
    from mockboard.modes.debate.motions import load_curated_motions

    return [{"number": m.number, "title": m.title, "text": m.text}
            for m in load_curated_motions().values()]


UTILITY_REPLAY = "replay"  # the `kind` access counts spoken second attempts under (Timbre+Prisma)
REPLAY_TTL_S = 900  # an attempt's token lives this long
REPLAY_MAX_AUDIO_S = 90  # the relay stops listening after this much audio
REPLAY_DEMO_ANSWER = (
    "Sabse pehle main prabhavit logon se baat karunga, kyunki unki zaroorat samajhna zaroori hai. "
    "Lekin iski ek keemat bhi hai: samay lagega."
)
UTILITY_MIC_CHECK = "mic_check"  # the `kind` access counts microphone checks under (Prisma)
UTILITY_LIKELY = "likely_questions"  # the `kind` access counts the likely-questions calls under


def _limit_message(decision, config: AccessConfig) -> str:
    """Why a likely-questions call got the standard hints: plain, polite, and true. It never says
    error, denied or blocked, and it says the interview is not affected."""
    if decision.reason == "likely_ip_full":
        why = "The model-written hints have reached their limit for this network"
    elif decision.reason == "likely_daily_full":
        why = "Today's model-written hints are all used"
    else:
        why = (f"You have used the {config.utility_per_visitor} model-written hint lists each "
               "visitor gets")
    return f"{why}, so this list comes with our standard hints. Your interviews are not affected."


def _static_path(asset: str) -> Path | None:  # access: an allowlist, so no lab page and no subpaths
    return _STATIC / asset if asset in _STATIC_FILES else None


def parse_form(data: dict[str, Any] | None, *, allow_default: bool) -> CandidateForm:
    """The page's application form. Only the known fields are read; no identifiers exist."""
    raw = {k: v for k, v in (data or {}).items() if k in FORM_FIELDS and v not in (None, "")}
    if not raw and allow_default:
        raw = dict(DEFAULT_FAKE_FORM)
    return CandidateForm.model_validate(_form_values(raw))


# The page's mode picker (Nov 7, owner): one server offers every mode, and each sitting says
# which it wants. `--mode` stays the default for a page that sends none.
SITTING_MODES = {"upsc": "upsc_interview", "debate": "debate", "partner": "study_partner"}
DEBATE_LANGUAGES = ("hinglish", "hindi", "english")
PARTNER_LANGUAGES = ("hinglish", "english", "hindi", "tanglish")


@dataclass(frozen=True)
class Sitting:
    """What one offer asked for: the mode and its own options. Holds no identifier."""

    mode: str  # a key of SITTING_MODES
    form: CandidateForm | None = None  # upsc
    motion: Any = None  # debate: the curated Motion
    side: str = "for"
    language: str = "hinglish"  # debate and partner; upsc reads the form's
    opponent: str = "logician"
    length: str = "quick"
    topic: str = ""  # partner

    @property
    def speech_language(self) -> str:
        """The interview language speech uses (Prisma's locale, Timbre's text filter)."""
        return self.form.interview_language if self.form is not None else self.language


class SittingError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_sitting(data: dict[str, Any] | None, cfg: AppConfig) -> Sitting:
    """The sitting an offer asks for, checked before any WebRTC work. Unknown keys are ignored;
    a bad value raises `SittingError` with plain-words problems for the page."""
    data = data if isinstance(data, dict) else {}
    default = "debate" if cfg.mode == "debate" else "upsc"
    mode = str(data.get("mode") or default)
    if mode not in SITTING_MODES:
        raise SittingError([f"mode: choose one of {', '.join(SITTING_MODES)}"])
    if mode == "upsc":
        try:
            return Sitting("upsc", form=parse_form(data.get("form"), allow_default=cfg.fake_llm))
        except ValidationError as e:
            raise SittingError(
                [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            ) from e
    if mode == "debate":
        from mockboard.modes.debate.motions import find_motion, load_curated_motions

        raw = data.get("debate") if isinstance(data.get("debate"), dict) else {}
        problems = []
        motion = cfg.motion
        if raw.get("motion") not in (None, ""):
            try:
                motion = find_motion(load_curated_motions(), str(raw["motion"]))
            except KeyError:
                problems.append("motion: choose one of the listed motions")
        if motion is None:
            problems.append("motion: choose a motion")
        side = str(raw.get("side") or cfg.side or "for")
        language = str(raw.get("language") or cfg.language)
        opponent = str(raw.get("opponent") or cfg.opponent)
        length = str(raw.get("length") or cfg.length)
        if side not in ("for", "against"):
            problems.append("side: for or against")
        if language not in DEBATE_LANGUAGES:
            problems.append(f"language: one of {', '.join(DEBATE_LANGUAGES)}")
        if opponent not in ("logician", "wit"):
            problems.append("opponent: logician or wit")
        if length not in ("quick", "standard"):
            problems.append("length: quick or standard")
        if problems:
            raise SittingError(problems)
        return Sitting("debate", motion=motion, side=side, language=language, opponent=opponent,
                       length=length)
    raw = data.get("partner") if isinstance(data.get("partner"), dict) else {}
    topic = " ".join(str(raw.get("topic") or "").split())
    language = str(raw.get("language") or "hinglish")
    problems = []
    if language not in PARTNER_LANGUAGES:
        problems.append(f"language: one of {', '.join(PARTNER_LANGUAGES)}")
    try:
        from mockboard.modes.partner.session import PartnerSetup

        PartnerSetup(topic=topic, language=language if not problems else "hinglish")
    except ImportError:
        problems.append("mode: the study partner is not available on this server")
    except (ValueError, ValidationError) as e:
        problems.append(f"topic: {e}" if topic else "topic: say what you want to talk about")
    if problems:
        raise SittingError(problems)
    return Sitting("partner", language=language, topic=topic)


def make_session(
    cfg: AppConfig, sitting: Sitting | CandidateForm | None, max_answers: int | None = None
) -> InterviewSession:
    """The same session the text CLI builds (cli.run), with a fresh trace file. `max_answers` is
    the admission decision's cap for the UPSC panel (None: uncapped); debate ignores it. A bare
    form (or None) is a sitting in the server's default mode, as before the mode picker."""
    from mockboard.modes.debate.motions import Side
    from mockboard.modes.debate.session import DebateSetup, Length, OpponentStyle
    from mockboard.modes.upsc_panel.setup import PanelSetup

    if not isinstance(sitting, Sitting):
        if cfg.mode == "debate":
            sitting = Sitting("debate", motion=cfg.motion, side=cfg.side, language=cfg.language,
                              opponent=cfg.opponent, length=cfg.length)
        else:
            sitting = Sitting("upsc", form=sitting)
    s = cfg.settings
    cards, _origin = load_cards_or_builtin(s.cards_dir)
    session_id = f"voice-{uuid.uuid4().hex[:10]}"
    recorder = TraceRecorder(s.trace_dir, session_id)
    llm = build_llm(s, cfg.fake_llm, cards, recorder)
    deps = build_deps(s, llm, cards=cards)
    form = sitting.form
    options: Any = PanelSetup(max_answers=max_answers)
    if sitting.mode == "debate":
        options = DebateSetup(motion=sitting.motion, user_side=Side(sitting.side),
                              length=Length(sitting.length), language=sitting.language,
                              opponent_style=OpponentStyle(sitting.opponent))
        form = None
    elif sitting.mode == "partner":
        from mockboard.modes.partner.session import PartnerSetup

        options = PartnerSetup(topic=sitting.topic, language=sitting.language)
        form = None
    session = InterviewSession(deps=deps, form=form, recorder=recorder,
                               analyzer=TurnAnalyzer(deps), session_id=session_id,
                               max_turns=cfg.max_turns, mode=SITTING_MODES[sitting.mode],
                               mode_options=options)
    session.sitting_language = sitting.speech_language  # read by make_speech
    return session


def _speech_language(session: InterviewSession) -> str:
    """The sitting's interview language: the form's, or the mode setup's (debate, partner)."""
    language = getattr(session, "sitting_language", None)
    if language:
        return language
    return session.form.interview_language if session.form else "hinglish"


def make_speech(cfg: AppConfig, session: InterviewSession, ledger: PlaybackLedger):
    """(stt, tts): ours on Gnani by default, or the fakes with --fake-speech."""
    if cfg.fake_speech:
        from mockboard.voice.fakes import FakeCommitSTT, FakeTTSService

        return FakeCommitSTT(), FakeTTSService(ledger=ledger, audible=True, voice=cfg.voice)
    from mockboard.speech.prisma_stt import PrismaSTTService
    from mockboard.speech.timbre_tts import TimbreTTSService

    s = cfg.settings
    keyterms, topic = speech_context(session) if s.stt_context_enabled else ([], None)
    # Ruling 31: x-api-request-id = <session id>-c<k>, so Gnani's ids match our trace.
    # UNVERIFIED: against the real Prisma and Timbre services (fake servers only so far).
    stt = PrismaSTTService.from_settings(
        s, keyterms=keyterms or None, topic=topic,
        lang_code=lang_code_for(_speech_language(session), s),
        service_kwargs={"request_id_prefix": session.session_id},
    )
    hinglish = _speech_language(session) == "hinglish"
    tts = TimbreTTSService.from_settings(s, voice=cfg.voice, sample_rate=s.tts_sample_rate,
                                         ledger=ledger,
                                         text_filter=hinglish_for_tts if hinglish else None)
    return stt, tts


async def run_session(
    webrtc_connection, cfg: AppConfig, form: Sitting | CandidateForm | None,
    max_answers: int | None = None,
) -> None:
    """One browser connection, until it disconnects or the session ends. `max_answers` is the
    answer cap the server's admission decision gave this connection (None: uncapped).

    The session is closed on every way out of here (the report done, a disconnect, a failure while
    the pipeline is being built), so the panel's checkpointer thread is never left behind: the saver
    is shared by every session, and one that is never closed leaks for the life of the server
    (Ruling 73 item 6)."""
    session = make_session(cfg, form, max_answers)
    try:
        await _serve(webrtc_connection, cfg, session)
    finally:
        try:
            await session.close()
        except Exception as e:  # never let a cleanup problem hide how the session ended
            logger.warning(f"[voice] closing session {session.session_id} failed: "
                           f"{type(e).__name__}: {e}")


async def _serve(webrtc_connection, cfg: AppConfig, session: InterviewSession) -> None:
    from pipecat.transports.base_transport import TransportParams
    from pipecat.workers.runner import WorkerRunner

    from mockboard.voice.cv2_shim import ensure_cv2
    from mockboard.voice.pipeline import build_voice_pipeline

    ensure_cv2()  # before the transport's module-level `import cv2` (defect E)
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

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
        mic_recorder=mic, voice_for=cfg.voice_for(),
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
        "tts_panel_voices": dict(cfg.panel_voices),
        "tts_voice_changes": list(getattr(tts, "voice_changes", []) or []),
        "tts_voice_refused": list(getattr(tts, "voice_refused", []) or []),
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


def _client_ip(request: Request) -> str:  # access:
    client = request.client
    return client.host if client is not None and client.host else "unknown"


def _stamp_cookie(response: Response, token: str | None, request: Request) -> Response:  # access:
    if not token:
        return response
    response.set_cookie(
        COOKIE, token, max_age=60 * 60 * 24 * 180, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return response


def _identity(request: Request, book: Limits) -> tuple[Identity, str | None]:  # access:
    """The visitor, and a cookie to set when they do not have a valid one yet."""
    cfg = book.config
    parsed = None
    if cfg.secret:
        parsed = read_token(request.cookies.get(COOKIE, ""), cfg.secret, codes=cfg.codes)
    token = None
    if parsed is None:
        visitor_id, reviewer = new_visitor_id(), False
        if cfg.enabled and cfg.secret:
            token = sign_token(visitor_id, False, cfg.secret)
    else:
        visitor_id, reviewer = parsed
    ip_hash = hash_ip(_client_ip(request), cfg.secret or "disabled")
    return Identity(visitor_id, ip_hash, reviewer), token


def _redeemed(identity: Identity) -> Identity:  # access:
    return Identity(identity.visitor_id, identity.ip_hash, True)


def create_app(cfg: AppConfig) -> FastAPI:
    # Localhost only, so no STUN/TURN servers: host candidates are enough (V1b adds them).
    handler = SmallWebRTCRequestHandler()
    pending_forms: dict[str, Sitting | None] = {}  # the sitting each offer asked for
    pending_counts: dict[str, Any] = {}  # access: Decisions, committed once connected
    # access: pc_id to `decision.max_answers`. Only `on_connection` writes it; `start_session`
    # reads it and passes it to the panel. A missing entry means capped.
    pending_limits: dict[str, int | None] = {}
    offer_ips: dict[str, str] = {}  # access: pc_id to IP hash, for in-flight offers
    offers_by_ip: Counter[str] = Counter()  # access: includes offers still being answered
    book = Limits(AccessConfig.from_env())  # access:

    def release_offer(pc_id: str) -> None:  # access: this offer is no longer in flight
        ip_hash = offer_ips.pop(pc_id, None)
        if ip_hash is not None:
            _release_ip(ip_hash)

    def _release_ip(ip_hash: str) -> None:  # access:
        offers_by_ip[ip_hash] -= 1
        if offers_by_ip[ip_hash] <= 0:
            del offers_by_ip[ip_hash]

    def forget_offer(pc_id: str) -> None:  # access: it ended with no session; keep nothing
        release_offer(pc_id)
        pending_forms.pop(pc_id, None)
        pending_counts.pop(pc_id, None)
        pending_limits.pop(pc_id, None)

    async def start_session(connection) -> None:  # only for connections that connected
        # access: count only after the peer connection is up. A reload that never
        # connects is not in this function, so it is not counted.
        pc_id = getattr(connection, "pc_id", "")
        release_offer(pc_id)
        decision = pending_counts.pop(pc_id, None)
        if decision is None or not decision.commit():
            forget_offer(pc_id)
            try:
                await connection.disconnect()
            except Exception:
                logger.warning("[access] could not close a connection past the limit")
            return
        # access: the cap this connection was admitted with. None is uncapped (a reviewer, or limits
        # off). No entry at all means capped, never uncapped: the server fails closed (Ruling 49).
        cap = pending_limits.get(pc_id, book.config.max_answers)
        try:
            await run_session(connection, cfg, pending_forms.pop(pc_id, None), max_answers=cap)
        finally:
            pending_limits.pop(pc_id, None)  # access: the session has read it

    gate = PageSessions(start_session, connect_timeout_s=cfg.settings.voice_connect_timeout_s,
                        on_unstarted=forget_offer)  # access: on_unstarted

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await gate.close_all()
        await handler.close()
        book.close()  # access:

    # access: no /docs or /openapi.json on a public server
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gate = gate  # for tests
    app.state.pending = {  # access: for tests
        "forms": pending_forms, "counts": pending_counts, "limits": pending_limits,
        "offer_ips": offer_ips, "offers_by_ip": offers_by_ip,
    }

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)  # no icon; avoids a 404 line in the log

    @app.get("/")
    async def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/guide")
    async def guide():
        """The guide: what is where, how to use each mode, what you can say (Nov 7, owner)."""
        return FileResponse(HERE / "static" / "guide.html")

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

    @app.get("/api/access")
    async def access_status(request: Request):
        # access: remaining interviews, answers per interview, and reviewer status
        identity, token = _identity(request, book)
        decision = book.admit(identity, datetime.now(UTC))
        return _stamp_cookie(JSONResponse(decision.public()), token, request)

    @app.post("/api/access")
    async def access_code(request: Request):
        # access: check a reviewer code before the interview, so a wrong one spends nothing
        try:
            body = await request.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        now = datetime.now(UTC)
        identity, token = _identity(request, book)
        result = "wrong"
        if isinstance(code, str):
            result = book.check_code(identity.ip_hash, code, now)
        if result == "ok":
            identity = _redeemed(identity)
            token = sign_token(identity.visitor_id, True, book.config.secret,
                               code=code.strip(), now=now)
        decision = book.admit(identity, now)
        answer = JSONResponse({**decision.public(), "code": result})
        return _stamp_cookie(answer, token, request)

    utility: dict[str, Any] = {}  # model client and prompt kit, built by the first admitted call

    def utility_model():
        """What a likely-questions call needs, built once: the client (real Nova, or the offline
        stand-in with --fake-llm), the prompt kit with the `summary` prompt, the members' names."""
        if not utility:
            from mockboard.brain.likely_questions import kit_with_summary

            s = cfg.settings
            cards, _origin = load_cards_or_builtin(s.cards_dir)
            recorder = TraceRecorder(s.trace_dir, "likely", enabled=False)
            deps = build_deps(s, build_llm(s, cfg.fake_llm, cards, recorder), cards=cards)
            rt = deps.mode(cfg.mode)
            utility.update(
                llm=deps.llm, kit=kit_with_summary(rt.kit, s.prompt_summary_version),
                names={p.id: p.name for p in rt.personas.values()},
            )
        return utility["llm"], utility["kit"], utility["names"]

    @app.post("/api/likely-questions")
    async def likely_questions_route(request: Request):
        """Utility A (P9, WEB_REQUESTS.md): `{ "form": {...} }` (the offer's form object) in,
        `{ "questions": [ { "member", "text", ... } ] }` out. Writing the hints on Nova costs
        credits, so the call goes through access admission and is counted before the model is
        called. When it is not admitted (limits reached) the questions still come back, with the
        standard hints and no model call, and a polite `limit` note; nothing here is an error."""
        from mockboard.brain.likely_questions import likely_questions

        if cfg.mode == "debate":
            return JSONResponse({"error": "mode"}, status_code=404)  # debate has no form
        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        data = body.get("form", (body.get("request_data") or {}).get("form"))
        try:
            form = parse_form(data if isinstance(data, dict) else None, allow_default=cfg.fake_llm)
        except ValidationError as e:
            problems = [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            return JSONResponse({"error": "form", "problems": problems}, status_code=400)
        identity, token = _identity(request, book)
        decision = book.admit_utility(identity, datetime.now(UTC), UTILITY_LIKELY)
        admitted = decision.allowed and decision.commit()  # counted BEFORE the model is called
        if admitted:
            llm, kit, names = utility_model()
            result = await likely_questions(form, names, llm=llm, kit=kit,
                                            timeout_s=cfg.settings.likely_questions_timeout_s)
        else:
            from mockboard.brain.personas import load_personas

            names = {p.id: p.name for p in load_personas().values()}
            result = await likely_questions(form, names)  # template hints: no call, no cost
        payload = result.as_json()
        if not admitted:
            payload["limit"] = {"reason": decision.reason or "likely_used",
                                "message": _limit_message(decision, book.config)}
        return _stamp_cookie(JSONResponse(payload), token, request)

    @app.post("/api/mic-check")
    async def mic_check_route(request: Request):
        """Upgrade #2: `?language=<interview language>` and a raw body of 16 kHz mono 16-bit PCM
        (at most `MAX_CHECK_SECONDS`). Prisma transcribes it with the locale the sitting will use,
        and the reply says what was heard, which words matched, and what to do. Nothing is stored.
        A real check spends a few seconds of Prisma, so it goes through access admission; when it
        is not admitted the page is told politely and the interview is not affected."""
        from mockboard.speech.mic_check import (
            CHECK_LINES,
            MAX_CHECK_SECONDS,
            MIN_SPEECH_RMS,
            lang_code_for,
            rms,
            score,
            transcribe_once,
        )

        if cfg.mode == "debate":
            return JSONResponse({"error": "mode"}, status_code=404)
        language = request.query_params.get("language", "hinglish")
        if language not in CHECK_LINES:
            return JSONResponse({"error": "language"}, status_code=400)
        pcm = await request.body()
        rate = cfg.settings.stt_sample_rate
        if len(pcm) > MAX_CHECK_SECONDS * rate * 2 or len(pcm) % 2:
            return JSONResponse({"error": "audio"}, status_code=413)
        lang_code = lang_code_for(language, cfg.settings)
        silent = rms(pcm) < MIN_SPEECH_RMS
        if silent:  # nothing to send: no Prisma call, no count
            return JSONResponse(score(language, "", lang_code=lang_code, silent=True).as_json())
        if cfg.fake_speech:  # the free demo: a fake "heard" line, labelled as such
            result = score(language, CHECK_LINES[language], lang_code=lang_code).as_json()
            return JSONResponse({**result, "demo": True})
        identity, token = _identity(request, book)
        decision = book.admit_utility(identity, datetime.now(UTC), UTILITY_MIC_CHECK)
        if not (decision.allowed and decision.commit()):  # counted BEFORE Prisma is called
            payload = {"limit": {"reason": decision.reason or "mic_check_used", "message": (
                "You have used the microphone checks for now. The interview itself is not "
                "affected, and you can begin whenever you are ready.")}}
            return _stamp_cookie(JSONResponse(payload), token, request)
        key = cfg.settings.gnani_api_key
        try:
            if key is None:
                raise ConnectionError("no speech key configured")
            heard, latency = await transcribe_once(
                pcm, cfg.settings, lang_code=lang_code, api_key=key.get_secret_value(),
            )
            result = score(language, heard, lang_code=lang_code, latency_ms=latency)
        except Exception as e:
            logger.warning(f"[mic-check] {type(e).__name__}")
            result = score(language, "", lang_code=lang_code, error=True)
        return _stamp_cookie(JSONResponse(result.as_json()), token, request)

    # --- Upgrade #3: a spoken second attempt at the weakest question --------------------------
    # One attempt = one admission count (real speech only), then at most one spoken question
    # (Timbre, in the member's voice) and one live listen (Prisma). The comparison is code-only.
    replay_attempts: dict[str, dict[str, Any]] = {}

    def _attempt(attempt_id: str) -> dict[str, Any] | None:
        now = time.monotonic()
        for key in [k for k, v in replay_attempts.items() if v["expires"] < now]:
            replay_attempts.pop(key, None)
        return replay_attempts.get(attempt_id)

    @app.post("/api/replay/start")
    async def replay_start(request: Request):
        from mockboard.brain.agenda import SEGMENT_MEMBERS
        from mockboard.speech.mic_check import CHECK_LINES

        if cfg.mode == "debate":
            return JSONResponse({"error": "mode"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        language = str(body.get("language") or "hinglish")
        if language not in CHECK_LINES:
            return JSONResponse({"error": "language"}, status_code=400)
        member = SEGMENT_MEMBERS.get(str(body.get("segment") or ""), "chairman")
        token = None
        if not cfg.fake_speech:
            identity, token = _identity(request, book)
            decision = book.admit_utility(identity, datetime.now(UTC), UTILITY_REPLAY)
            if not (decision.allowed and decision.commit()):  # counted BEFORE any speech call
                payload = {"limit": {"reason": decision.reason or "replay_used", "message": (
                    "You have used the spoken practice attempts for now. You can still type a "
                    "second answer below and compare it with your first.")}}
                return _stamp_cookie(JSONResponse(payload), token, request)
        if len(replay_attempts) > 500:
            return JSONResponse({"error": "busy"}, status_code=503)
        pick = cfg.voice_for()
        attempt_id = uuid.uuid4().hex
        replay_attempts[attempt_id] = {
            "expires": time.monotonic() + REPLAY_TTL_S, "speak": 1, "listen": 1, "repair": 1,
            "language": language, "voice": pick(member) if pick else cfg.voice,
            "demo": cfg.fake_speech,
        }
        response = JSONResponse({"attempt": attempt_id, "demo": cfg.fake_speech})
        return _stamp_cookie(response, token, request) if token else response

    @app.post("/api/replay/{attempt_id}/question")
    async def replay_question(attempt_id: str, request: Request):
        from mockboard.speech.replay_speech import MAX_TTS_CHARS, synthesize, wav_bytes

        attempt = _attempt(attempt_id)
        if attempt is None or attempt["speak"] < 1:
            return JSONResponse({"error": "attempt"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        question = str((body or {}).get("question") or "").strip() if isinstance(body, dict) else ""
        if not question or len(question) > MAX_TTS_CHARS:
            return JSONResponse({"error": "question"}, status_code=400)
        attempt["speak"] -= 1
        if attempt["demo"]:
            return Response(status_code=204)  # the free demo has no voice; the page shows text
        key = cfg.settings.gnani_api_key
        try:
            if key is None:
                raise ConnectionError("no speech key configured")
            spoken = hinglish_for_tts(question) if attempt["language"] == "hinglish" else question
            pcm = await synthesize(spoken, attempt["voice"], cfg.settings,
                                   api_key=key.get_secret_value())
        except Exception as e:
            logger.warning(f"[replay] question audio {type(e).__name__}")
            return JSONResponse({"error": "speech"}, status_code=502)
        return Response(wav_bytes(pcm, cfg.settings.tts_sample_rate), media_type="audio/wav")

    @app.websocket("/ws/replay/{attempt_id}")
    async def replay_listen(websocket: WebSocket, attempt_id: str):
        """Binary messages: 16 kHz mono 16-bit PCM as the candidate speaks. A text message
        `{"type": "done"}` ends the answer. One JSON reply: `{"heard", "latency_ms"}`."""
        from mockboard.speech.mic_check import lang_code_for
        from mockboard.speech.replay_speech import PrismaRelay

        await websocket.accept()
        attempt = _attempt(attempt_id)
        if attempt is None or attempt["listen"] < 1:
            await websocket.close(code=1008)
            return
        attempt["listen"] -= 1
        limit = REPLAY_MAX_AUDIO_S * cfg.settings.stt_sample_rate * 2
        relay = None
        try:
            if not attempt["demo"]:
                key = cfg.settings.gnani_api_key
                if key is None:
                    raise ConnectionError("no speech key configured")
                relay = PrismaRelay(cfg.settings,
                                    lang_code=lang_code_for(attempt["language"], cfg.settings),
                                    api_key=key.get_secret_value())
                await relay.open()
            received = 0
            while received < limit:
                message = await asyncio.wait_for(websocket.receive(), REPLAY_MAX_AUDIO_S + 30)
                if message.get("type") == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data:
                    received += len(data)
                    if relay is not None:
                        await relay.push(data)
                elif message.get("text") and "done" in message["text"]:
                    break
            if relay is None:
                await websocket.send_json({"heard": REPLAY_DEMO_ANSWER, "demo": True})
            else:
                heard, latency = await relay.finish()
                await websocket.send_json({"heard": heard, "latency_ms": latency})
        except WebSocketDisconnect:
            return
        except Exception as e:
            logger.warning(f"[replay] listen {type(e).__name__}")
            with contextlib.suppress(Exception):
                await websocket.send_json({"error": "speech"})
        finally:
            if relay is not None:
                await relay.close()
            with contextlib.suppress(Exception):
                await websocket.close()

    @app.post("/api/replay/{attempt_id}/repair")
    async def replay_repair(attempt_id: str, request: Request):
        """Likely-misheard words in the second attempt (repair.v1, one Nova call per attempt).
        The transcript is never edited; the page shows the pairs beside it."""
        from mockboard.brain.heard_repair import MAX_CHARS, repairs_for

        attempt = _attempt(attempt_id)
        if attempt is None or attempt["repair"] < 1:
            return JSONResponse({"error": "attempt"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        heard = str(body.get("heard") or "")[:4000]
        question = str(body.get("question") or "")[:400]
        if not heard.strip() or attempt["demo"]:  # the free demo makes no model call
            return JSONResponse({"likely": []})
        attempt["repair"] -= 1
        llm, kit, _ = utility_model()
        from dataclasses import replace as _replace

        kit = _replace(kit, versions={**kit.versions, "repair": cfg.settings.prompt_repair_version})
        found = await repairs_for([heard], llm=llm, kit=kit, question=question)
        pairs = [{"heard": p.heard, "likely": p.likely} for p in found
                 if len(p.likely) <= MAX_CHARS]
        return JSONResponse({"likely": pairs})

    @app.post("/api/replay/compare")
    async def replay_compare(request: Request):
        """Free and code-only: the first answer and the second attempt, compared on observable
        parts of an answer. No model call and no score."""
        from mockboard.brain.second_attempt import MAX_ANSWER_CHARS, compare_attempts

        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "body"}, status_code=400)
        before = str(body.get("before") or "")[:MAX_ANSWER_CHARS]
        after = str(body.get("after") or "")[:MAX_ANSWER_CHARS]
        return JSONResponse(compare_attempts(before, after).as_json())

    @app.post("/api/offer")
    async def offer(request: Request):
        body = await request.json()
        data = body.get("request_data") or body.get("requestData") or {}
        try:
            form = parse_sitting(data, cfg)  # the sitting: mode, form or setup
        except SittingError as e:
            return JSONResponse({"error": "form", "problems": e.problems}, status_code=400)
        # access: admission hook, before any WebRTC work
        now = datetime.now(UTC)
        identity, token = _identity(request, book)
        code = data.get("access_code")
        if book.config.enabled and isinstance(code, str) and code.strip():
            result = book.check_code(identity.ip_hash, code, now)
            if result != "ok":  # a wrong code never spends an interview
                refused = JSONResponse({"error": "code", "reason": result}, status_code=403)
                return _stamp_cookie(refused, token, request)
            identity = _redeemed(identity)
            token = sign_token(identity.visitor_id, True, book.config.secret,
                               code=code.strip(), now=now)
        decision = book.admit(identity, now)
        if book.config.enabled and not decision.allowed:
            refused = JSONResponse(
                {"error": "limit", "reason": decision.reason, "used": decision.used,
                 "limit": decision.limit},
                status_code=429,
            )
            return _stamp_cookie(refused, token, request)
        # access: each offer holds a peer connection for the connect timeout
        ip_hash = identity.ip_hash
        if book.config.enabled and offers_by_ip[ip_hash] >= MAX_OFFERS_PER_IP:
            return _stamp_cookie(JSONResponse({"error": "busy"}, status_code=429), token, request)
        offers_by_ip[ip_hash] += 1  # access: before the await, so parallel offers all count
        webrtc_request = SmallWebRTCRequest.from_dict(
            {k: body.get(k) for k in ("sdp", "type", "pc_id", "restart_pc")}
        )

        page_id = str(data.get("page_id") or "")[:64] or None

        async def on_connection(connection):
            # Defect D: no pipeline (and no Gnani connection) until this connection connects;
            # a newer offer from the same page replaces it.
            pending_forms[connection.pc_id] = form
            pending_counts[connection.pc_id] = decision  # access: commit once it connects
            pending_limits[connection.pc_id] = decision.max_answers  # access: read in start_session
            offer_ips[connection.pc_id] = ip_hash  # access:
            claimed.append(connection.pc_id)  # access:
            await gate.offer(connection, page_id)

        claimed: list[str] = []  # access:
        try:
            answer = await handler.handle_web_request(
                request=webrtc_request, webrtc_connection_callback=on_connection
            )
        finally:
            if not claimed:  # access: no connection took the reservation
                _release_ip(ip_hash)
        return _stamp_cookie(JSONResponse(answer), token, request)  # access: the cookie

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


def check_panel_voices(cfg: AppConfig) -> None:
    """Every voice of the panel table must be in the Timbre model's catalog, checked before the
    server starts (a wrong one would otherwise fail on a real line). Exits with the reason."""
    from mockboard.speech.gnani_protocol import check_voice

    for member, voice in cfg.panel_voices.items():
        try:
            check_voice(cfg.settings.tts_model, voice)
        except ValueError as e:
            sys.exit(f"TTS_PANEL_VOICES: {member!r} -> {e} (TTS_MODEL is "
                     f"{cfg.settings.tts_model}). Fix the voice, or set TTS_PANEL_VOICES={{}} for "
                     "one voice.")


def _refuse_exposure(cfg: AppConfig, access: AccessConfig) -> None:  # access:
    """Real services on a public address, with limits off, would spend credits for anyone."""
    if access.enabled or cfg.host in _LOOPBACK or (cfg.fake_llm and cfg.fake_speech):
        return
    sys.exit(f"Refusing to start: real services on {cfg.host} with access limits off. Set "
             "ACCESS_ENABLED=true in .env, or use a loopback host.")


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
    p.add_argument("--max-turns", type=int, default=None,
                   help="debate only: hard cap on candidate turns (default 9). The UPSC panel sets "
                        "its own cap from its agenda and refuses this flag; a test user's cap "
                        "comes from ACCESS_MAX_ANSWERS (Ruling 76 item 3)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--trace-dir", help="override TRACE_DIR (default traces)")
    p.add_argument("--record-audio", action="store_true",
                   help="save YOUR microphone audio to traces/<session>.mic.wav on this computer "
                        "(off by default; for `python -m mockboard.voice.stt_ab`)")
    a = p.parse_args(argv)
    if MODES[a.mode] != "debate" and a.max_turns is not None:
        p.error("--max-turns does not apply to the UPSC panel: the agenda sets the turn cap, and "
                "a test user's cap comes from ACCESS_MAX_ANSWERS (--max-turns is for debate)")
    settings = Settings()
    if a.trace_dir:
        settings = settings.model_copy(update={"trace_dir": Path(a.trace_dir)})
    # P8: the panel's voices. An explicit --voice means one voice for everyone; debate has no seats.
    panel_voices = {} if a.voice or MODES[a.mode] == "debate" else settings.panel_voices()
    cfg = AppConfig(mode=MODES[a.mode], settings=settings, fake_llm=a.fake_llm,
                    fake_speech=a.fake_speech, voice=a.voice or settings.tts_voice,
                    max_turns=9 if a.max_turns is None else a.max_turns, length=a.length,
                    language=a.language,
                    opponent=a.opponent, host=a.host, port=a.port,
                    record_audio=a.record_audio, panel_voices=panel_voices)
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
    check_panel_voices(cfg)
    _check_keys(cfg)
    try:  # access: a bad ACCESS_ value stops startup here
        access = AccessConfig.from_env()
        _refuse_exposure(cfg, access)
        app = create_app(cfg)
    except ValueError as e:
        sys.exit(f"Access settings: {e}")
    host, port = cfg.host, cfg.port
    shown = json.dumps(cfg.public(), ensure_ascii=False)
    print(f"\nMock Board voice app: open http://{host}:{port} in Chrome. Ctrl+C stops it.")
    print(f"Config: {shown}\nTraces and reports: {cfg.settings.trace_dir}/\n")
    # access: uvicorn's access log writes raw client addresses
    uvicorn.run(app, host=host, port=port, access_log=not access.enabled)


if __name__ == "__main__":
    main()
