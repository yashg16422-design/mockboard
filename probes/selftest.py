"""Offline self-test for the probes. No network, no credits, no AWS calls.

Run it from the mockboard folder with `python -m probes.selftest`. It checks:
* Prisma header building, VAD range checks, 1,024-byte framing, real-time pacing,
  the update_settings frame, and message parsing (including partial detection);
* a full Prisma probe session against a fake local WebSocket server that replays
  doc-shaped messages, so the sender/receiver timing code really runs;
* Timbre request bodies, voice-catalog checks, WebSocket and SSE parsing, WAV-header
  stripping, plus a full WebSocket run against a fake server and an SSE run against a
  mocked HTTP transport;
* Nova stream timing, forced-tool JSON validation, and throttling backoff with fake
  botocore errors; the `--app-prompts` mode's requests (built by the app's own builders)
  and a whole app-prompts run against a fake bedrock-runtime client;
* WAV reading/resampling, percentiles, the RESULTS.md writer (to a temp file), and the
  fact-card files against both schema.json and the FactCard model.
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

import numpy as np

from probes import gnani_ws as g

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def deco(fn: Callable[[], None]) -> Callable[[], None]:
        try:
            out = fn()
            if asyncio.iscoroutine(out):
                asyncio.run(out)
            RESULTS.append((name, True, ""))
        except Exception as e:
            RESULTS.append(
                (name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            )
        return fn

    return deco


def tone_pcm(seconds: float, rate: int = 16000, silence_tail_s: float = 0.0) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    x = (0.3 * 32767 * np.sin(2 * np.pi * 220 * t)).astype("<i2")
    tail = np.zeros(int(silence_tail_s * rate), dtype="<i2")
    return np.concatenate([x, tail]).tobytes()


# ---------------------------------------------------------------------------
# Prisma: pure logic
# ---------------------------------------------------------------------------


@check("stt headers: defaults from Settings, exact doc names, partials follow the flag")
def _() -> None:
    s = g.Settings(_env_file=None)
    h = g.build_stt_headers(g.STTConfig.from_settings(s), "KEY")
    assert h["x-api-key-id"] == "KEY"
    assert h["language_code"] == s.stt_lang_code and h["x-sample-rate"] == str(s.stt_sample_rate)
    assert h["format"] == s.stt_format and float(h["x-max-speech-s"]) == s.stt_max_speech_s
    assert h["model"] == s.stt_model and h["x-idle-timeout-sec"] == str(s.stt_idle_timeout_s)
    assert ("x-partials" in h) == s.feature_prisma_partials
    assert g.redact_headers(h)["x-api-key-id"] == "***"
    h2 = g.build_stt_headers(g.STTConfig(), "KEY")
    assert h2["language_code"] == "en-IN" and h2["x-max-speech-s"] == "15"
    assert "x-partials" not in h2


@check("stt headers: VAD values, partials and bias list")
def _() -> None:
    cfg = g.STTConfig(
        lang_code="hi-IN",
        vad_threshold=0.5,
        min_silence_ms=700,
        min_speech_ms=100,
        min_segment_ms=120,
        max_speech_s=15.0,
        partials=True,
        bias_list=["UPSC", "Naxalism"],
        bias_score=1.5,
    )
    h = g.build_stt_headers(cfg, "K")
    assert h["x-vad-threshold"] == "0.5" and h["x-min-silence-ms"] == "700"
    assert h["x-min-speech-ms"] == "100" and h["x-min-segment-ms"] == "120"
    assert h["x-partials"] == "true"
    assert json.loads(h["bias_list"]) == ["UPSC", "Naxalism"] and h["bias_score"] == "1.5"


@check("stt headers: out-of-range VAD and bad bias words are rejected")
def _() -> None:
    for kw in (
        {"vad_threshold": 1.5},
        {"min_silence_ms": 50},
        {"max_speech_s": 200.0},
        {"min_segment_ms": 2000},
        {"sample_rate": 22050},
        {"fmt": "pretty"},
    ):
        try:
            g.build_stt_headers(g.STTConfig(**kw), "K")
        except ValueError:
            continue
        raise AssertionError(f"accepted {kw}")
    for words in (["two words"], ["abc123"], ["x"] * 101):
        try:
            g.validate_bias_words(words)
        except ValueError:
            continue
        raise AssertionError(f"accepted bias {words[:2]}")


@check("framing: exact 1,024-byte frames, zero-padded tail, 32 ms cadence at 16 kHz")
def _() -> None:
    pcm = b"\x01\x02" * 1500  # 3,000 bytes -> 3 frames, last padded
    frames = list(g.iter_frames(pcm))
    assert [len(f) for f in frames] == [1024, 1024, 1024]
    assert frames[-1][3000 - 2048 :] == b"\x00" * (3072 - 3000)
    assert abs(g.frame_duration_s(16000) - 0.032) < 1e-9
    assert abs(g.frame_duration_s(8000) - 0.064) < 1e-9
    deadlines = [d for _, d in g.pace_frames(4, 16000, t0=100.0)]
    assert [round(d - 100.0, 3) for d in deadlines] == [0.0, 0.032, 0.064, 0.096]


@check("update_settings frame matches the documented example")
def _() -> None:
    doc = {"type": "update_settings", "bias": {"list": ["Vachana", "EMI"], "score": 1.5}}
    assert json.loads(g.update_settings_frame(["Vachana", "EMI"], 1.5)) == doc
    sub = json.loads(g.update_settings_frame(substitution={"enabled": False}))
    assert sub == {"type": "update_settings", "substitution": {"enabled": False}}


@check("stt parsing: doc-shaped messages, partial detection, junk frames kept")
def _() -> None:
    m = g.parse_stt_message(
        json.dumps(
            {
                "type": "transcript",
                "text": "haan ji",
                "audio_duration_ms": 900,
                "segment_id": "s1",
                "segment_index": 0,
                "latency": 320,
                "timestamp": "2026-10-25T10:00:00Z",
            }
        ),
        arrived=1.0,
    )
    assert m.type == "transcript" and m.text == "haan ji" and not m.is_partial
    assert g.parse_stt_message('{"type":"partial_transcript","text":"ha"}').is_partial
    assert g.parse_stt_message('{"type":"transcript","text":"ha","is_final":false}').is_partial
    assert g.parse_stt_message("not json").type == "non_json"
    assert g.parse_stt_message(b"\x00\x01").type == "binary"


# ---------------------------------------------------------------------------
# Prisma: full session against a fake server
# ---------------------------------------------------------------------------


async def _fake_prisma(ws) -> None:
    """Replays the documented flow: connected -> (audio) -> processing -> transcript.
    Emits one segment per ~0.25 s of received audio, plus a fake partial if the client
    sent x-partials, and answers update_settings with settings_updated."""
    hdr = ws.request.headers
    assert hdr["x-api-key-id"] == "TESTKEY" and hdr["language_code"] == "en-IN"
    partials = hdr.get("x-partials") == "true"
    await ws.send(
        json.dumps(
            {
                "type": "connected",
                "message": "STT service ready — VAD service connected",
                "config": {"sample_rate": 16000, "chunk_size": 512},
            }
        )
    )
    got, seg = 0, 0
    async for msg in ws:
        if isinstance(msg, str):
            obj = json.loads(msg)
            if obj.get("type") == "update_settings":
                await ws.send(
                    json.dumps(
                        {
                            "type": "settings_updated",
                            "config": {
                                "language_code": "en-IN",
                                "format": "verbatim",
                                "itn_native_numerals": False,
                                "substitution_enabled": False,
                                "bias_word_count": len(obj["bias"]["list"]),
                                "bias_score": obj["bias"]["score"],
                            },
                        }
                    )
                )
            continue
        assert len(msg) == 1024, f"bad frame size {len(msg)}"
        got += 1
        if partials and got == 3:
            await ws.send(json.dumps({"type": "partial_transcript", "text": "umm"}))
        if got % 8 == 0 and seg < 2:  # 8 frames = 256 ms
            await ws.send(json.dumps({"type": "processing", "timestamp": "t"}))
            await asyncio.sleep(0.02)
            await ws.send(
                json.dumps(
                    {
                        "type": "transcript",
                        "text": f"umm segment {seg}",
                        "audio_duration_ms": 256,
                        "segment_id": f"s{seg}",
                        "segment_index": seg,
                        "latency": 20,
                        "timestamp": "t",
                    }
                )
            )
            seg += 1


@check("prisma probe: full session vs fake server (cadence, update_settings, partials)")
async def _() -> None:
    from websockets.asyncio.server import serve

    from probes.prisma_probe import run_session, summarise

    async with serve(_fake_prisma, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"
        pcm = tone_pcm(0.5)
        for partials in (False, True):
            cfg = g.STTConfig(partials=partials)
            t = time.monotonic()
            res = await run_session(
                url,
                g.build_stt_headers(cfg, "TESTKEY"),
                pcm,
                16000,
                update_frame=g.update_settings_frame(["UPSC"], 1.5),
                update_at_s=0.1,
                trailing_silence_s=0.2,
                idle_timeout_s=0.4,
                partials_header=partials,
                quiet=True,
            )
            elapsed = time.monotonic() - t
            assert res.error is None, res.error
            # 0.5 s audio + 0.2 s silence = 22 frames; real-time pacing => >= ~0.67 s.
            assert elapsed >= 0.6, f"sent too fast ({elapsed:.2f}s): not real-time"
            assert len(res.transcripts) == 2 and res.raw_text == "umm segment 0 umm segment 1"
            assert (len(res.partials) > 0) == partials
            assert res.settings_updated_ms() is not None
            row = summarise(res, measured_speech_ms=500, clip_ms=500)
            assert row["segments"] == 2 and row["audio_duration_ms sum"] == 512
            assert row["processing->transcript ms (mean)"] != "n/a"


# ---------------------------------------------------------------------------
# Timbre
# ---------------------------------------------------------------------------


@check("tts request: documented body shape, raw PCM, voice catalog check")
def _() -> None:
    body = g.TTSRequest(text="Namaste", voice="Poorvi").body()
    assert body["model"] == "timbre-v2.5" and body["language"] == "hi-en"
    assert body["audio_config"] == {
        "sample_rate": 16000,
        "num_channels": 1,
        "sample_width": 2,
        "encoding": "linear_pcm",
        "container": "raw",
    }
    for bad in (
        dict(voice="NotAVoice"),
        dict(voice="Shivani"),
        dict(voice="Poorvi", speed=2.0),
        dict(voice="Poorvi", sample_rate=11025),
        dict(voice="Poorvi", language="xx"),
    ):
        try:
            g.TTSRequest(text="x", **bad).body()
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    g.TTSRequest(text="x", voice="Shivani", model="timbre-v3.0").body()
    assert g.build_tts_headers("K") == {"X-API-Key-ID": "K", "Content-Type": "application/json"}


@check("tts parsing: WebSocket JSON, SSE events, WAV header stripping")
def _() -> None:
    pcm = b"\x10\x00" * 100
    b64 = base64.b64encode(pcm).decode()
    c = g.parse_tts_ws_message(
        json.dumps({"type": "audio", "data": {"chunk_index": 1, "audio": b64, "is_final": False}})
    )
    assert c.audio == pcm and c.chunk_index == 1 and not c.is_terminal
    done = g.parse_tts_ws_message(
        '{"type": "complete", "data": {"chunk_index": 2, "audio": "", "is_final": true}}'
    )
    assert done.is_terminal and not done.is_audio
    assert g.parse_tts_ws_message('{"type": "error", "message": "Invalid text"}').is_terminal
    sse = [
        "event: start",
        'data: {"status": "started", "text": "x"}',
        "",
        ": keep-alive",
        "",
        "event: chunk",
        f'data: {{"chunk_index": 1, "audio": "{b64}", "is_final": false}}',
        "",
        "event: complete",
        'data: {"chunk_index": 2, "audio": "", "is_final": true}',
        "",
    ]
    evs = list(g.parse_sse_lines(iter(sse)))
    assert [e.type for e in evs] == ["start", "chunk", "complete"]
    assert evs[1].audio == pcm and evs[2].is_terminal
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(pcm)
    assert g.strip_wav_header(buf.getvalue()) == pcm
    assert g.strip_wav_header(pcm) == pcm


async def _fake_timbre(ws) -> None:
    assert ws.request.headers["X-API-Key-ID"] == "TESTKEY"
    async for msg in ws:
        req = json.loads(msg)
        assert req["voice"] == "Poorvi" and req["audio_config"]["container"] == "raw"
        await ws.send(
            json.dumps({"type": "start", "message": "Streaming started", "request_id": "req_test"})
        )
        for i in range(3):
            await asyncio.sleep(0.01)
            a = base64.b64encode(b"\x01\x00" * 160).decode()
            await ws.send(
                json.dumps(
                    {"type": "audio", "data": {"chunk_index": i, "audio": a, "is_final": False}}
                )
            )
        await ws.send(
            json.dumps(
                {"type": "complete", "data": {"chunk_index": 3, "audio": "", "is_final": True}}
            )
        )


@check("timbre probe: WebSocket cold+warm vs fake server, SSE vs mocked transport")
async def _() -> None:
    import httpx
    from websockets.asyncio.server import serve

    from probes.timbre_probe import run_sse, run_ws

    req = g.TTSRequest(text="Namaste", voice="Poorvi")
    async with serve(_fake_timbre, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        results = await run_ws(
            f"ws://127.0.0.1:{port}", g.build_tts_headers("TESTKEY"), req, warm_repeats=1
        )
    assert [r.transport for r in results] == ["ws-cold", "ws-warm"], [r.error for r in results]
    for r in results:
        assert r.error is None and r.chunks == 3 and len(r.pcm) == 960
        assert r.first_byte_ms is not None and r.total_ms >= r.first_byte_ms
        assert r.row(16000)["audio_ms"] == 30

    a = base64.b64encode(b"\x02\x00" * 160).decode()
    body = (
        'event: start\ndata: {"status": "started", "text": "Namaste"}\n\n'
        f'event: chunk\ndata: {{"chunk_index": 1, "audio": "{a}", "is_final": false}}\n\n'
        'event: complete\ndata: {"chunk_index": 2, "audio": "", "is_final": true}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key-ID"] == "TESTKEY"
        assert json.loads(request.content)["voice"] == "Poorvi"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await run_sse(
            client, "https://fake.invalid/api/v1/tts/sse", g.build_tts_headers("TESTKEY"), req
        )
    assert r.error is None and r.chunks == 1 and len(r.pcm) == 320 and r.total_ms is not None

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"success": False, "error": {"type": "RATE_LIMIT_ERROR", "message": "slow down"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
        r = await run_sse(client, "https://fake.invalid/sse", g.build_tts_headers("K"), req)
    assert r.error and r.error.startswith("HTTP 429")


# ---------------------------------------------------------------------------
# Nova
# ---------------------------------------------------------------------------


@check("nova: stream TTFT/total timing from fake ConverseStream events")
def _() -> None:
    from probes.nova_probe import Throttled, consume_stream

    clock = iter([1.25, 1.5, 2.0]).__next__  # first delta, first sentence end, stream end
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Achha, "}}},
        {"contentBlockDelta": {"delta": {"text": "batayiye?"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"outputTokens": 7, "cacheReadInputTokens": 1500}}},
    ]
    s = consume_stream(events, t_req=1.0, clock=clock)
    assert s.ttft_ms == 250 and s.first_sentence_ms == 500 and s.total_ms == 1000
    assert s.text == "Achha, batayiye?" and s.stop_reason == "end_turn"
    assert s.output_tokens == 7 and s.cache_read == 1500
    try:
        consume_stream([{"throttlingException": {"message": "slow"}}], 0.0)
    except Throttled:
        pass
    else:
        raise AssertionError("in-stream throttle not raised")


@check("nova: forced-tool JSON validity (valid / schema_invalid / no_tool_use)")
def _() -> None:
    from probes.nova_probe import TOOL, classify_tool_response

    def resp(payload):
        content = (
            [{"toolUse": {"toolUseId": "t1", "name": TOOL.name, "input": payload}}]
            if payload is not None
            else [{"text": "hi"}]
        )
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": "tool_use",
        }

    good = {
        "stance": "balanced",
        "claim_en": "Exampleland tourism is 12 percent of GDP",
        "value": 12,
        "unit": "percent",
        "year": 2023,
        "reasons": ["jobs", "over-tourism"],
    }
    assert classify_tool_response(resp(good))[0] == "valid"
    assert classify_tool_response(resp({**good, "year": "twenty"}))[0] == "schema_invalid"
    assert classify_tool_response(resp({**good, "extra": 1}))[0] == "schema_invalid"
    assert classify_tool_response(resp(json.dumps(good)))[0] == "valid"  # JSON-string input
    assert classify_tool_response(resp(None))[0] == "no_tool_use"


@check("nova: request shape reuses the app builder (forced toolChoice, temp 0, cachePoint)")
def _() -> None:
    from mockboard.brain.llm import BedrockNovaClient, PromptBlock
    from probes.nova_probe import CANDIDATE_ANSWER, TOOL, synthetic_system_prefix

    app = BedrockNovaClient(
        region="ap-southeast-2", model_id="amazon.nova-pro-v1:0", client=object()
    )
    req = app.build_tool_request(
        [PromptBlock(synthetic_system_prefix(), cache=True)], CANDIDATE_ANSWER, TOOL, 400
    )
    assert req["toolConfig"]["toolChoice"] == {"tool": {"name": "record_analysis"}}
    assert req["inferenceConfig"]["temperature"] == 0.0
    assert req["system"][1] == {"cachePoint": {"type": "default"}}
    assert len(synthetic_system_prefix()) >= 1500 * 4  # comfortably above the 1K-token minimum


@check("nova: throttling backoff counts throttles and re-raises other errors")
def _() -> None:
    from botocore.exceptions import ClientError

    from probes.nova_probe import Counters, call_with_backoff

    def throttle():
        return ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Too many"}}, "Converse"
        )

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise throttle()
        return "ok"

    c = Counters()
    assert call_with_backoff(flaky, c, base_s=0.001) == "ok" and c.throttles == 2
    c2 = Counters()
    try:
        call_with_backoff(
            lambda: (_ for _ in ()).throw(throttle()), c2, max_retries=2, base_s=0.001
        )
    except RuntimeError:
        assert c2.throttles == 3
    else:
        raise AssertionError("expected give-up")
    try:
        call_with_backoff(
            lambda: (_ for _ in ()).throw(
                ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse"
                )
            ),
            Counters(),
        )
    except ClientError:
        pass
    else:
        raise AssertionError("non-throttle error swallowed")


@check("nova: fatal errors (no creds, access denied) stop a phase; throttles do not")
def _() -> None:
    from botocore.exceptions import ClientError, NoCredentialsError

    from probes.nova_probe import is_fatal

    def ce(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "x"}}, "Converse")

    assert is_fatal(NoCredentialsError()) and is_fatal(ce("AccessDeniedException"))
    assert is_fatal(ce("ValidationException"))
    assert not is_fatal(ce("ThrottlingException")) and not is_fatal(TimeoutError())


class _FakeRuntime:
    """Stands in for boto3's bedrock-runtime client in the --app-prompts checks.

    `converse` replies with a listener tool call (the first call is throttled once, the
    third has an invalid field type); `converse_stream` replies with a short Hinglish line.
    Every request is kept so the checks can inspect the exact request shape."""

    def __init__(self) -> None:
        self.tool_requests: list[dict] = []
        self.stream_requests: list[dict] = []
        self._throttled = False

    def converse(self, **req):
        from botocore.exceptions import ClientError

        if not self._throttled:
            self._throttled = True
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse"
            )
        self.tool_requests.append(req)
        payload = {
            "stance": "Tourism matters but needs balance",
            "reasons": ["jobs"],
            "claims": [],
            "balance": "balanced",
            "honest_unknown": False,
            "concedes_error": False,
            "garbled": False,
        }
        if len(self.tool_requests) == 3:
            payload["reasons"] = "not a list"  # schema-invalid
        name = req["toolConfig"]["toolChoice"]["tool"]["name"]
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"toolUse": {"toolUseId": "t", "name": name, "input": payload}}],
                }
            },
            "stopReason": "tool_use",
            "usage": {
                "inputTokens": 1200,
                "outputTokens": 60,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 0,
            },
        }

    def converse_stream(self, **req):
        self.stream_requests.append(req)
        events = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "Achha, "}}},
            {"contentBlockDelta": {"delta": {"text": "theek hai. Ab yeh "}}},
            {"contentBlockDelta": {"delta": {"text": "bataiye, kyun?"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 1300, "outputTokens": 12}}},
        ]
        return {"stream": iter(events)}


def _app_ctx():
    from mockboard.config import Settings
    from probes.nova_probe import load_app_context

    return load_app_context(Settings(_env_file=None))


@check("nova --app-prompts: requests come from the app's builders (prefix, tool, stream)")
def _() -> None:
    from mockboard.brain.llm import BedrockNovaClient
    from mockboard.brain.tools import LISTENER_TOOL
    from probes.nova_probe import SAMPLE_TURNS, actor_request, listener_request

    ctx = _app_ctx()
    app = BedrockNovaClient(
        region="ap-southeast-2", model_id="amazon.nova-pro-v1:0", client=object()
    )
    moves = {t.move for t in SAMPLE_TURNS}
    assert {"probe", "counter", "fact_challenge", "move_on", "reassure"} <= moves
    for turn in SAMPLE_TURNS:
        lreq = listener_request(app, ctx, turn, k=0)
        # The real cached prefix (persona pack + cards), then a checkpoint, then the role.
        assert lreq["system"][0] == {"text": ctx.kit.shared_prefix}
        assert lreq["system"][1] == {"cachePoint": {"type": "default"}}
        assert lreq["toolConfig"]["toolChoice"] == {"tool": {"name": LISTENER_TOOL.name}}
        assert lreq["inferenceConfig"]["temperature"] == 0.0
        assert lreq["inferenceConfig"]["stopSequences"] == ["</tool>"]
        assert "<candidate_speech>" in lreq["messages"][0]["content"][0]["text"]
        areq = actor_request(app, ctx, turn)
        assert "toolConfig" not in areq
        assert areq["system"][0] == {"text": ctx.kit.shared_prefix}
        assert areq["system"][1] == {"cachePoint": {"type": "default"}}
        user = areq["messages"][0]["content"][0]["text"]
        assert "<board_context>" in user and turn.move in user
        if turn.move == "fact_challenge":
            # actor.v2 gets the pre-rendered figures, never the card text.
            assert "<fact_challenge>" in user and "12 percent" in user
            assert "TEST CARD" not in user


@check("nova --app-prompts: run with a fake client counts validity, tokens, throttles")
def _() -> None:
    from mockboard.brain.llm import BedrockNovaClient
    from probes.nova_probe import run_app_prompts

    ctx = _app_ctx()
    fake = _FakeRuntime()
    app = BedrockNovaClient(region="ap-southeast-2", model_id="amazon.nova-pro-v1:0", client=fake)
    with tempfile.TemporaryDirectory() as d:
        jl = Path(d) / "nova_app.jsonl"
        rows, notes = run_app_prompts(
            fake, app, ctx, n=4, gap_s=0, jsonl_path=jl, backoff_base_s=0.001
        )
        lines = [json.loads(x) for x in jl.read_text().splitlines()]
    lis, act = rows
    assert lis["ok"] == 4 and lis["valid_json"].startswith("3/4 (invalid 1")
    assert lis["throttles"].startswith("1 (fail after") and lis["errors"] == 0
    assert lis["mean in/out tokens"] == "1200 / 60"
    assert act["ok"] == 4 and act["mean in/out tokens"] == "1300 / 12"
    assert act["ttft p50/p95 ms"] != "n/a / n/a" and act["throttles"] == "0"
    assert len(fake.tool_requests) == 4 and len(fake.stream_requests) == 4
    assert [x["call"] for x in lines] == ["listener"] * 4 + ["actor"] * 4
    assert lines[4]["text"] == "Achha, theek hai. Ab yeh bataiye, kyun?"
    assert lines[4]["first_sentence_ms"] is not None
    assert any("Per-call rows" in n for n in notes)


@check("nova --app-prompts: listener validity uses the app model (truncated, no tool)")
def _() -> None:
    from probes.nova_probe import classify_listener_response

    assert classify_listener_response({"stopReason": "max_tokens"})[0] == "truncated"
    empty = {"output": {"message": {"content": [{"text": "hi"}]}}, "stopReason": "end_turn"}
    assert classify_listener_response(empty)[0] == "no_tool_use"


@check("nova: --profile, --region, --origin parse; app-prompts defaults to 20 calls")
def _() -> None:
    from probes.nova_probe import build_parser

    a = build_parser().parse_args(
        ["--app-prompts", "--region", "ap-southeast-2", "--profile", "mb", "--origin", "x"]
    )
    assert a.app_prompts and a.profile == "mb" and a.region == "ap-southeast-2"
    assert a.origin == "x" and a.n is None  # main() turns None into 20 for --app-prompts


# ---------------------------------------------------------------------------
# Common helpers and data
# ---------------------------------------------------------------------------


@check("wav: 16 kHz mono accepted, other formats rejected or resampled")
def _() -> None:
    from probes.common import WavFormatError, read_wav_16k_mono, speech_ms, write_wav

    with tempfile.TemporaryDirectory() as d:
        ok = write_wav(Path(d) / "ok.wav", tone_pcm(1.0, silence_tail_s=1.0), 16000)
        pcm, rate = read_wav_16k_mono(ok)
        assert rate == 16000 and len(pcm) == 64000
        assert 900 <= speech_ms(pcm, rate) <= 1100
        low = write_wav(Path(d) / "8k.wav", tone_pcm(1.0, rate=8000), 8000)
        try:
            read_wav_16k_mono(low)
        except WavFormatError as e:
            assert "16000 Hz mono" in str(e) and "ffmpeg" in str(e)
        else:
            raise AssertionError("8 kHz accepted without --resample")
        pcm2, rate2 = read_wav_16k_mono(low, resample=True)
        assert rate2 == 16000 and abs(len(pcm2) - 32000) <= 4


@check("results: percentile and RESULTS.md table writer (temp file)")
def _() -> None:
    from probes.common import append_results, percentile

    vals = list(range(1, 101))
    assert percentile(vals, 50) == 50 and percentile(vals, 95) == 95 and percentile([], 50) is None
    with tempfile.TemporaryDirectory() as d:
        p = append_results(
            "Self-test", [{"a": 1, "b": "x|y"}], ["note"], {"k": 1}, path=Path(d) / "R.md"
        )
        text = p.read_text()
        assert "# Probe results" in text and "## Self-test" in text
        assert "| a | b |" in text and "x\\|y" in text and "- note" in text


@check("cards: files validate against schema.json and the FactCard model; all synthetic")
def _() -> None:
    import jsonschema

    from mockboard.brain.cards import load_cards
    from mockboard.brain.schemas import FactCard

    cards_dir = Path(__file__).resolve().parents[1] / "data" / "cards"
    schema = json.loads((cards_dir / "schema.json").read_text())
    card_props = set(schema["$defs"]["card"]["properties"])
    assert card_props == set(FactCard.model_fields), card_props ^ set(FactCard.model_fields)
    cs = load_cards(cards_dir, ["tourism", "internal_security"])
    assert len(cs.cards) == 6 and cs.all_synthetic
    for c in cs.cards.values():
        assert c.statement.startswith("TEST CARD:") and c.source_url is None
    # The schema itself must refuse an unsourced real card.
    bad = {"topic": "tourism", "cards": [{"id": "x", "statement": "s", "synthetic_example": False}]}
    try:
        jsonschema.validate(bad, schema)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("schema accepted a real card with no source")


def main() -> None:
    width = max(len(n) for n, _, _ in RESULTS)
    for name, ok, err in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}")
        if err:
            print("      " + err.replace("\n", "\n      "))
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} checks passed (offline, no credits spent).")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
