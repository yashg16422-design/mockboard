"""Print a per-turn timeline from an echo-spike JSONL log.

    python -m spikes.echo_bot.summarize                      # newest log in spikes/echo_bot/logs
    python -m spikes.echo_bot.summarize path/to/echo_X.jsonl

All times are milliseconds since the session started. For each user turn it shows when Silero
heard the end of speech, when each Prisma final arrived (with Prisma's own fields), when Smart
Turn ended the turn, and when the echo's first audio left Timbre and reached the output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"


def _short(name: str) -> str:
    return name.split("#")[0]


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _frame_line(r: dict) -> str | None:
    frame, src, dst, down = r["frame"], _short(r["src"]), _short(r["dst"]), r["dir"] == "down"
    if src == "VADProcessor" and down and frame == "VADUserStartedSpeakingFrame":
        return "Silero: speech started"
    if src == "VADProcessor" and down and frame == "VADUserStoppedSpeakingFrame":
        return f"Silero: speech stopped (stop_secs={r.get('stop_secs')})"
    if frame == "TranscriptionFrame" and dst == "UserTurnProcessor":
        res = r.get("stt_result") or {}
        extra = {k: res[k] for k in ("latency", "audio_duration_ms", "segment_id") if k in res}
        return f"Prisma final: {r.get('text')!r} {extra or ''}"
    if frame == "PrismaProcessingFrame" and dst == "UserTurnProcessor":
        return "Prisma: segment ended, transcript pending"
    if frame == "InterruptionFrame" and src == "UserTurnProcessor" and down:
        return "interruption broadcast"
    if frame == "TTSAudioRawFrame" and r.get("burst_start") and dst.endswith("OutputTransport"):
        return "first TTS audio of a burst reaches the output"
    if frame.startswith("BotSt") and src.endswith("OutputTransport") and dst == "Pipeline":
        return frame.removesuffix("Frame")
    if frame == "ErrorFrame":
        return f"ERROR {r.get('error')}"
    return None


def summarize(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for r in rows:
        kind, ms = r["kind"], r["ms"]
        fields = {k: v for k, v in r.items() if k not in ("wall", "ms", "kind")}
        if kind == "frame":
            text = _frame_line(r)
        elif kind == "event":
            text = f"event {r['name']}" + (f" ({r['strategy']})" if r.get("strategy") else "")
            text += f" {r['args']}" if r.get("args") else ""
        else:  # echo, echo_skipped, transcript_outside_turn, session_start/end, ...
            text = f"{kind} {fields}"
        if text:
            out.append(f"{ms:9.0f}  {text}")
    return out


def main(argv: list[str]) -> None:
    if argv:
        path = Path(argv[0])
    else:
        logs = sorted(LOG_DIR.glob("echo_*.jsonl"))
        if not logs:
            sys.exit(f"No logs in {LOG_DIR}. Run the spike first.")
        path = logs[-1]
    print(f"# {path}")
    print("\n".join(summarize(load(path))))


if __name__ == "__main__":
    main(sys.argv[1:])
