"""Voice lab (ruling 38): hear the same three board lines with every Timbre setting that could
make the board sound less robotic, and pick by ear.

    python -m mockboard.voice.lab            # http://localhost:7861, real Timbre (credits)
    python -m mockboard.voice.lab --fake     # the same page with tones instead of Timbre (free)

What can be compared (Gnani docs read Oct 31, 2026: tts-websocket.md, tts-sse.md,
available-voices.md):

* **Model and voice.** `timbre-v2.5` (42 voices, each with one preferred language) and
  `timbre-v3.0` ("Expressive text-to-speech": 13 voices, every voice speaks all 10 languages,
  inline audio tags). Voices are not shared between models. Six voices suited to a Hindi /
  Hinglish board: Poorvi (v2.5, the only voice Gnani recommends for `hi-en`, the board's voice
  today), Vikrant and Urmila (v2.5 Hindi), Avinash, Yashraj and Vasudha (v3.0).
* **Language.** `auto` (detect from the script), `hi-IN`, `hi-en` (Hinglish; in the docs but
  refused by gnani-vachana 0.7.9, which the lab does not use) and `en-IN`.
* **Script.** The board's Latin-script Hinglish, Devanagari with the English words left in
  Latin (what Prisma v3.0 `codemix` produces), and all-Devanagari.
* **Request unit.** One request for the whole line, or one per sentence played back to back
  (what the board does today: Timbre's prosody restarts at each sentence).
* **Sample rate.** 16 kHz (today) or 24 kHz.

Nothing is rendered until you press a play button; each render is cached under
`traces/voice_lab/` (`<id>.wav` and `<id>.json` with the settings and timings), so playing it
again is free. Renders run one at a time (Timbre allows one active request per key). The page
shows the characters sent so far; Timbre bills per character (rate UNVERIFIED). The key is read
from GNANI_API_KEY, used only server-side, and never printed or saved.
UNVERIFIED: everything against the real Timbre (tested against a fake server only).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Module level, not inside create_app: FastAPI must resolve `Request` in the route signature,
# which fails for names imported in a function under `from __future__ import annotations`.
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from mockboard.config import Settings
from mockboard.speech.gnani_protocol import (
    TTS_LANGUAGES,
    TTS_MODELS,
    TTS_SAMPLE_RATES,
    TTSRequest,
    build_tts_headers,
    check_voice,
    parse_tts_ws_message,
    strip_wav_header,
)
from mockboard.voice.lines import split_sentences

HERE = Path(__file__).resolve().parent

# Three board lines: the Chairperson's opener, a member's open topic question (ruling 36: open,
# never a quiz) and the debate opponent's rebuttal. Each in three scripts.
LINES: list[dict[str, str]] = [
    {
        "speaker": "Chairperson (opener)",
        "latin": "Namaste, aap baithiye. Aaj hum aapse kuch sawaal poochhenge, aap aaram se "
                 "jawab dijiye. Shuru karte hain: aap civil services mein kyun aana chahte hain?",
        "deva_mixed": "नमस्ते, आप बैठिए। आज हम आपसे कुछ सवाल पूछेंगे, आप आराम से जवाब दीजिए। "
                      "शुरू करते हैं: आप civil services में क्यों आना चाहते हैं?",
        "deva_full": "नमस्ते, आप बैठिए। आज हम आपसे कुछ सवाल पूछेंगे, आप आराम से जवाब दीजिए। "
                     "शुरू करते हैं: आप सिविल सर्विसेज़ में क्यों आना चाहते हैं?",
    },
    {
        "speaker": "Board member (topic question)",
        "latin": "Theek hai. Ab ek alag vishay par chalte hain. Pahadi ilaakon mein tourism ka "
                 "environment par kya asar padta hai, aur use kaise santulit kiya ja sakta hai?",
        "deva_mixed": "ठीक है। अब एक अलग विषय पर चलते हैं। पहाड़ी इलाक़ों में tourism का "
                      "environment पर क्या असर पड़ता है, और उसे कैसे संतुलित किया जा सकता है?",
        "deva_full": "ठीक है। अब एक अलग विषय पर चलते हैं। पहाड़ी इलाक़ों में टूरिज़्म का "
                     "एनवायरनमेंट पर क्या असर पड़ता है, और उसे कैसे संतुलित किया जा सकता है?",
    },
    {
        "speaker": "Debate opponent (rebuttal)",
        "latin": "Main aapki baat se sahmat nahi hoon. Agar schools mein mobile phone poori "
                 "tarah band kar diye jaayein, toh bachche technology se door ho jaayenge. Kya "
                 "yeh sach mein unke hit mein hai?",
        "deva_mixed": "मैं आपकी बात से सहमत नहीं हूँ। अगर schools में mobile phone पूरी तरह बंद "
                      "कर दिए जाएँ, तो बच्चे technology से दूर हो जाएँगे। क्या यह सच में उनके "
                      "हित में है?",
        "deva_full": "मैं आपकी बात से सहमत नहीं हूँ। अगर स्कूलों में मोबाइल फ़ोन पूरी तरह बंद "
                     "कर दिए जाएँ, तो बच्चे टेक्नोलॉजी से दूर हो जाएँगे। क्या यह सच में उनके "
                     "हित में है?",
    },
]

# (model, voice, note) from available-voices.md.
VOICES: list[tuple[str, str, str]] = [
    ("timbre-v2.5", "Poorvi", "female; Hinglish (hi-en), natural bilingual; the board today"),
    ("timbre-v2.5", "Vikrant", "male; Hindi (hi-IN), warm, genuine conversation"),
    ("timbre-v2.5", "Urmila", "female; Hindi (hi-IN), refined, formal"),
    ("timbre-v3.0", "Avinash", "male; firm, measured"),
    ("timbre-v3.0", "Yashraj", "male; warm, narrative"),
    ("timbre-v3.0", "Vasudha", "female; expressive, composed"),
]
LANGUAGES: dict[str, str] = {
    "auto": "auto: detect from the script (Gnani's advice for mixed text)",
    "hi-IN": "hi-IN: Hindi",
    "hi-en": "hi-en: Hinglish (docs only; the board cannot send it yet)",
    "en-IN": "en-IN: Indian English",
}
SCRIPTS: dict[str, str] = {
    "latin": "Latin (as the board writes today)",
    "deva_mixed": "Devanagari, English words in Latin",
    "deva_full": "all Devanagari",
}
UNITS: dict[str, str] = {
    "line": "one request for the whole line",
    "sentence": "one request per sentence",
}
RATES = (16000, 24000)
BASELINE = {"script": "latin", "model": "timbre-v2.5", "voice": "Poorvi", "language": "auto",
            "unit": "sentence", "rate": 16000}  # what the board sends today
_ID = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class Variant:
    line: int
    script: str
    model: str
    voice: str
    language: str
    unit: str
    rate: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Variant:
        v = cls(line=int(d["line"]), script=str(d["script"]), model=str(d["model"]),
                voice=str(d["voice"]), language=str(d["language"]), unit=str(d["unit"]),
                rate=int(d["rate"]))
        v.validate()
        return v

    def validate(self) -> None:
        if not 0 <= self.line < len(LINES):
            raise ValueError(f"line must be 0-{len(LINES) - 1}")
        if self.script not in SCRIPTS:
            raise ValueError(f"script must be one of {sorted(SCRIPTS)}")
        if self.model not in TTS_MODELS:
            raise ValueError(f"model must be one of {TTS_MODELS}")
        check_voice(self.model, self.voice)
        if self.language not in TTS_LANGUAGES:
            raise ValueError(f"language must be one of {TTS_LANGUAGES}")
        if self.unit not in UNITS:
            raise ValueError(f"unit must be one of {sorted(UNITS)}")
        if self.rate not in TTS_SAMPLE_RATES:
            raise ValueError(f"rate must be one of {TTS_SAMPLE_RATES}")

    @property
    def id(self) -> str:
        blob = json.dumps(asdict(self) | {"text": self.text}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def text(self) -> str:
        return LINES[self.line][self.script]

    def pieces(self) -> list[str]:
        return [self.text] if self.unit == "line" else split_sentences(self.text)

    def note(self) -> str:
        return (f"{self.model} · {self.voice} · {self.language} · {SCRIPTS[self.script]} · "
                f"{UNITS[self.unit]} · {self.rate // 1000} kHz")

    def env(self) -> str:
        """The .env lines that make the board sound like this (script is the brain's choice)."""
        return (f"TTS_MODEL={self.model} TTS_VOICE={self.voice} TTS_LANGUAGE={self.language} "
                f"TTS_SAMPLE_RATE={self.rate} TTS_REQUEST_UNIT={self.unit}")


@dataclass
class Render:
    pcm: bytes = b""
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def error(self) -> str | None:
        return next((r["error"] for r in self.requests if r.get("error")), None)


async def synthesize(variant: Variant, *, key: str, url: str, connect=None,
                     timeout_s: float = 20.0) -> Render:
    """One WebSocket; the variant's pieces one request at a time, each until `complete`."""
    if connect is None:
        from websockets.asyncio.client import connect
    out = Render()
    async with connect(url, additional_headers=build_tts_headers(key), close_timeout=5) as ws:
        for piece in variant.pieces():
            body = TTSRequest(text=piece, voice=variant.voice, model=variant.model,
                              language=variant.language, sample_rate=variant.rate).body()
            row: dict[str, Any] = {"chars": len(piece), "first_byte_ms": None,
                                   "total_ms": None, "error": None}
            out.requests.append(row)
            t0 = time.monotonic()
            await ws.send(json.dumps(body, ensure_ascii=False))
            while True:
                chunk = parse_tts_ws_message(await asyncio.wait_for(ws.recv(), timeout_s))
                if chunk.audio:
                    if row["first_byte_ms"] is None:
                        row["first_byte_ms"] = round((time.monotonic() - t0) * 1000)
                    out.pcm += strip_wav_header(chunk.audio)
                if chunk.type == "error":
                    row["error"] = str(chunk.raw.get("message") or chunk.raw)[:300]
                    break
                if chunk.is_terminal:
                    break
            row["total_ms"] = round((time.monotonic() - t0) * 1000)
            if row["error"]:
                break
    if len(out.pcm) % 2:
        out.pcm = out.pcm[:-1]
    return out


def fake_synthesize(variant: Variant) -> Render:
    """Tones instead of Timbre: about 330 ms a word, a pitch per voice, 150 ms between
    per-sentence requests. Free; for checking the page."""
    out = Render()
    freq = 180 + 40 * [v for _, v, _ in VOICES].index(variant.voice)
    for i, piece in enumerate(variant.pieces()):
        if i:
            out.pcm += b"\x00\x00" * int(0.15 * variant.rate)
        n = int(0.33 * len(piece.split()) * variant.rate)
        out.pcm += b"".join(int(3000 * math.sin(2 * math.pi * freq * k / variant.rate))
                            .to_bytes(2, "little", signed=True) for k in range(n))
        out.requests.append({"chars": len(piece), "first_byte_ms": 0, "total_ms": 0,
                             "error": None})
    return out


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class VoiceLab:
    def __init__(self, settings: Settings, *, out_dir: Path, fake: bool = False,
                 url: str | None = None, connect=None):
        self.settings = settings
        self.out_dir = Path(out_dir)
        self.fake = fake
        self.url = url or settings.gnani_tts_ws_url
        self._connect = connect
        secret = settings.gnani_api_key
        self._key = secret.get_secret_value().strip() if secret is not None else ""
        self._lock = asyncio.Lock()  # one Timbre request at a time
        self.chars_sent = 0
        self.renders = 0

    def has_key(self) -> bool:
        return bool(self._key) and not self._key.startswith("replace-me")

    def public(self) -> dict[str, Any]:
        return {
            "fake": self.fake,
            "lines": [{"speaker": ln["speaker"], **{s: ln[s] for s in SCRIPTS}} for ln in LINES],
            "voices": [{"model": m, "voice": v, "note": n} for m, v, n in VOICES],
            "languages": LANGUAGES, "scripts": SCRIPTS, "units": UNITS, "rates": list(RATES),
            "baseline": BASELINE, "chars_sent": self.chars_sent,
            "board": {"model": self.settings.tts_model, "voice": self.settings.tts_voice,
                      "language": self.settings.tts_language,
                      "rate": self.settings.tts_sample_rate,
                      "unit": self.settings.tts_request_unit},
        }

    def wav_path(self, render_id: str) -> Path | None:
        if not _ID.match(render_id):
            return None
        path = self.out_dir / f"{render_id}.wav"
        return path if path.exists() else None

    async def render(self, variant: Variant) -> dict[str, Any]:
        rid = variant.id
        meta_path = self.out_dir / f"{rid}.json"
        if meta_path.exists() and (self.out_dir / f"{rid}.wav").exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta | {"cached": True, "chars_sent": self.chars_sent}
        async with self._lock:
            if self.fake:
                result = fake_synthesize(variant)
            else:
                if not self.has_key():
                    raise RuntimeError("GNANI_API_KEY is not set (.env); or run with --fake")
                result = await synthesize(variant, key=self._key, url=self.url,
                                          connect=self._connect)
            self.chars_sent += sum(r["chars"] for r in result.requests)
            self.renders += 1
        meta = {
            "id": rid, "url": f"/api/audio/{rid}", "settings": asdict(variant),
            "note": variant.note(), "env": variant.env(), "text": variant.text,
            "requests": result.requests, "error": result.error, "fake": self.fake,
            "seconds": round(len(result.pcm) / 2 / variant.rate, 2),
        }
        if result.pcm and not result.error:  # only complete renders are cached
            self.out_dir.mkdir(parents=True, exist_ok=True)
            write_wav(self.out_dir / f"{rid}.wav", result.pcm, variant.rate)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        elif not result.error:
            meta["error"] = "Timbre sent no audio"
        print(f"[lab] {'fake ' if self.fake else ''}render {rid}: {variant.note()} | "
              f"{meta['seconds']} s | first byte "
              f"{[r['first_byte_ms'] for r in result.requests]} ms | "
              f"{self.chars_sent} characters sent so far"
              + (f" | error: {meta['error']}" if meta["error"] else ""), flush=True)
        return meta | {"cached": False, "chars_sent": self.chars_sent}


def create_app(lab: VoiceLab) -> FastAPI:
    app = FastAPI()

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/")
    async def index():
        return FileResponse(HERE / "static" / "lab.html")

    @app.get("/api/config")
    async def config():
        return lab.public()

    @app.post("/api/render")
    async def render(request: Request):
        try:
            variant = Variant.from_dict(await request.json())
        except (KeyError, TypeError, ValueError) as e:
            return JSONResponse({"error": f"bad settings: {e}"}, status_code=400)
        try:
            return await lab.render(variant)
        except Exception as e:  # shown on the page; the key is never in these messages
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)

    @app.get("/api/audio/{render_id}")
    async def audio(render_id: str):
        path = lab.wav_path(render_id)
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, media_type="audio/wav")

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m mockboard.voice.lab",
                                description="Mock Board voice lab (ruling 38), localhost only.")
    p.add_argument("--fake", action="store_true", help="tones instead of Timbre (free)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=7861)
    p.add_argument("--out", help="where renders are cached (default TRACE_DIR/voice_lab)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    a = parse_args(argv)
    settings = Settings()
    out = Path(a.out) if a.out else Path(settings.trace_dir) / "voice_lab"
    lab = VoiceLab(settings, out_dir=out, fake=a.fake)
    if not a.fake and not lab.has_key():
        print("GNANI_API_KEY is not set (.env). Set it, or run with --fake to check the page.",
              file=sys.stderr)
        return 1
    mode = "FAKE (tones, free)" if a.fake else "REAL Timbre (each new render spends credits)"
    print(f"\nMock Board voice lab: open http://{a.host}:{a.port} in Chrome. Ctrl+C stops it.")
    print(f"Mode: {mode}. Renders are cached in {out}/\n")
    uvicorn.run(create_app(lab), host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
