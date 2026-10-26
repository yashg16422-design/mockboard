"""Helpers shared by the probes: WAV reading/writing, percentiles, and RESULTS.md tables."""

from __future__ import annotations

import json
import math
import socket
import wave
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

PROBES_DIR = Path(__file__).resolve().parent
RESULTS_DIR = PROBES_DIR / "results"
AUDIO_DIR = RESULTS_DIR / "audio"
RESULTS_MD = RESULTS_DIR / "RESULTS.md"


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------


class WavFormatError(ValueError):
    pass


def read_wav_16k_mono(path: Path, resample: bool = False) -> tuple[bytes, int]:
    """Return (s16le PCM bytes, sample_rate) for a 16 kHz mono 16-bit WAV.

    Any other format is rejected with a clear message, unless `resample=True`, in which
    case stereo is averaged to mono and the rate is converted with scipy's polyphase
    resampler (only 16-bit input is accepted either way).
    """
    try:
        with wave.open(str(path), "rb") as w:
            ch, width, rate, n = (
                w.getnchannels(),
                w.getsampwidth(),
                w.getframerate(),
                w.getnframes(),
            )
            pcm = w.readframes(n)
    except wave.Error as e:
        raise WavFormatError(
            f"{path}: not a plain PCM WAV ({e}). Convert it first with:\n"
            f"  ffmpeg -i {path} -ac 1 -ar 16000 -sample_fmt s16 out16k.wav"
        ) from e
    if width != 2:
        raise WavFormatError(
            f"{path}: sample width is {8 * width}-bit; Prisma needs 16-bit. Convert with:\n"
            f"  ffmpeg -i {path} -ac 1 -ar 16000 -sample_fmt s16 out16k.wav"
        )
    if ch == 1 and rate == 16000:
        return pcm, rate
    if not resample:
        raise WavFormatError(
            f"{path}: {rate} Hz, {ch} channel(s). The probe needs 16000 Hz mono. Either pass "
            f"--resample, or convert with:\n"
            f"  ffmpeg -i {path} -ac 1 -ar 16000 -sample_fmt s16 out16k.wav"
        )
    import numpy as np
    from scipy.signal import resample_poly

    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        g = math.gcd(16000, rate)
        x = resample_poly(x, 16000 // g, rate // g)
    y = np.clip(np.round(x), -32768, 32767).astype("<i2")
    return y.tobytes(), 16000


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return path


def speech_ms(pcm: bytes, sample_rate: int, frame_ms: int = 30, rel_db: float = -35.0) -> int:
    """Rough measured speech duration: count 30 ms frames whose RMS is within `rel_db` of
    the loudest frame. This is an energy gate, not a real VAD; it is only a sanity check
    against Prisma's `audio_duration_ms`."""
    import numpy as np

    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = int(sample_rate * frame_ms / 1000)
    if len(x) < n:
        return 0
    frames = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((frames**2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms / rms.max())
    return int((db > rel_db).sum() * frame_ms)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0-100). Returns None for an empty list."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, math.ceil(p / 100 * len(vals)) - 1))
    return vals[k]


def fmt_ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f}"


# ---------------------------------------------------------------------------
# RESULTS.md
# ---------------------------------------------------------------------------


def markdown_table(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return "_no rows_\n"
    cols = list(dict.fromkeys(k for r in rows for k in r))
    esc = lambda v: str("" if v is None else v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(esc(r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join(out) + "\n"


def append_results(
    probe: str,
    rows: Sequence[dict[str, Any]],
    notes: Sequence[str] = (),
    params: dict[str, Any] | None = None,
    path: Path = RESULTS_MD,
) -> Path:
    """Append one dated section to RESULTS.md (creating the file with a header if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# Probe results\n\nEach probe run appends a dated section below. "
            "Numbers are from the machine and network named in each section.\n",
            encoding="utf-8",
        )
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    parts = [f"\n## {probe} — {stamp}\n"]
    if params:
        parts.append("Parameters: `" + json.dumps(params, ensure_ascii=False, default=str) + "`\n")
    parts.append("\n" + markdown_table(rows))
    for n in notes:
        parts.append(f"\n- {n}")
    parts.append("\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(parts))
    return path


def origin_host() -> str:
    """The machine a probe runs on, for labelling results (laptop vs EC2 matters a lot)."""
    try:
        return socket.gethostname() or "unknown-host"
    except OSError:
        return "unknown-host"


def log_line(t0: float, t: float, kind: str, payload: Any) -> str:
    """One console log line: elapsed ms since t0, kind, compact payload."""
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    if len(body) > 400:
        body = body[:400] + "..."
    return f"[{(t - t0) * 1000:9.1f} ms] {kind:<18} {body}"
