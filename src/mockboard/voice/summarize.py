"""Summarise a voice session's trace for sharing: timings and counts, no transcript text.

    python -m mockboard.voice.summarize                 # newest traces/voice-*.jsonl
    python -m mockboard.voice.summarize traces/voice-abc.jsonl
    python -m mockboard.voice.summarize --all           # every voice session in traces/

For each turn it prints the latency chain from the candidate's end of speech to the board's
first audio, split into the stages the trace records (vad_end, commit_sent, turn_end,
move_ready, actor_first_sentence, tts_first_byte, playback_start), plus what happened
(move, barge-in, reopened turn, late finals, deadline fallbacks), then p50/p95 over the turns
and the session-level speech stats. Nothing the candidate or the board said is printed, and
no key is ever in a trace, so the output is safe to paste back to the team.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# (label, from stage, to stage)
SPANS = [
    ("stop->commit", "vad_end", "commit_sent"),
    ("commit->turn_end", "commit_sent", "turn_end"),
    ("turn_end->move", "turn_end", "move_ready"),
    ("move->1st sentence", "move_ready", "actor_first_sentence"),
    ("sentence->tts byte", "actor_first_sentence", "tts_first_byte"),
    ("tts byte->playback", "tts_first_byte", "playback_start"),
    ("STOP->FIRST AUDIO", "vad_end", "playback_start"),
]


def load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def span(stages: dict, a: str, b: str) -> float | None:
    if a in stages and b in stages:
        return round(stages[b] - stages[a], 1)
    return None


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    return round(statistics.quantiles(s, n=100, method="inclusive")[int(q) - 1], 1)


def summarize(rows: list[dict]) -> list[str]:
    turns = [r for r in rows if r.get("turn_id") and "voice" in r]
    start = next((r for r in rows if r.get("event") == "session_start"), {})
    sess = next((r for r in rows if r.get("event") == "voice_session"), {})
    out = [f"session {rows[0].get('session_id', '?') if rows else '?'}  mode="
           f"{start.get('mode', 'upsc_interview')}  turns={len(turns)}"]
    if sess:
        out.append(
            f"stt requested={sess.get('stt_model_requested')} served={sess.get('stt_model_served')}"
            f"  fake_llm={sess.get('fake_llm')} fake_speech={sess.get('fake_speech')}"
            f"  voice={sess.get('tts_voice')}"
        )
        if sess.get("tts_model"):  # ruling 38 settings (sessions from Oct 31 on)
            out.append(f"tts model={sess.get('tts_model')} language={sess.get('tts_language')}"
                       f" rate={sess.get('tts_sample_rate')} unit={sess.get('tts_request_unit')}")
    head = f"{'turn':<5}{'move':<17}" + "".join(f"{lbl:>20}" for lbl, _, _ in SPANS)
    out += ["", "per turn (ms; '-' = stage not reached):", head]
    collected: dict[str, list[float]] = {lbl: [] for lbl, _, _ in SPANS}
    flags_total = {"interrupted": 0, "reopened": 0, "late_finals": 0, "deadline_fallbacks": 0,
                   "actor_fallbacks": 0, "commit_timeouts": 0, "final_wait_ran_out": 0}
    final_waits: list[float] = []
    for r in turns:
        st, v = r.get("stages_ms", {}), r.get("voice", {})
        cells = []
        for lbl, a, b in SPANS:
            value = span(st, a, b)
            cells.append(f"{'-' if value is None else value:>20}")
            if value is not None:
                collected[lbl].append(value)
        flags = []
        if v.get("interrupted"):
            flags.append(f"barge-in (heard {v.get('played_ms', 0):.0f}/"
                         f"{v.get('audio_ms', 0):.0f} ms)")
            flags_total["interrupted"] += 1
        if v.get("turn_reopened"):
            flags.append(f"reopened x{v['turn_reopened']}")
            flags_total["reopened"] += 1
        if v.get("late_finals"):
            flags.append(f"late finals {len(v['late_finals'])}")
            flags_total["late_finals"] += len(v["late_finals"])
        if r.get("fallback_reason"):
            flags.append("deadline/analysis fallback")
            flags_total["deadline_fallbacks"] += 1
        if r.get("actor_fallback"):
            flags.append(f"actor fallback ({r.get('actor_fallback_kind')})")
            flags_total["actor_fallbacks"] += 1
        commit = v.get("commit") or {}
        if commit:
            flags.append(f"commit {commit.get('reply')}; waited {commit.get('final_wait_ms')} ms "
                         f"for finals ({'all in' if commit.get('settled') else 'ran out'})")
            flags_total["commit_timeouts"] += commit.get("reply") in ("timeout", "not_sent")
            flags_total["final_wait_ran_out"] += commit.get("settled") is False
            if commit.get("final_wait_ms") is not None:
                final_waits.append(commit["final_wait_ms"])
        if v.get("guard_stopped"):
            flags.append("guard stopped a sentence")
        out.append(f"{r['turn_id']:<5}{r.get('move', '?'):<17}" + "".join(cells))
        if flags:
            out.append(f"{'':5}{'; '.join(flags)}")
    out += ["", f"{'stage':<22}{'n':>4}{'p50':>9}{'p95':>9}{'max':>9}"]
    for lbl, values in collected.items():
        if values:
            out.append(f"{lbl:<22}{len(values):>4}{pct(values, 50):>9}{pct(values, 95):>9}"
                       f"{max(values):>9}")
    if final_waits:  # ruling 34: tune STT_FINAL_WAIT_MS from this
        out.append(f"{'final wait (stop->finals)':<22}{len(final_waits):>4}"
                   f"{pct(final_waits, 50):>9}{pct(final_waits, 95):>9}{max(final_waits):>9}")
    out += ["", "counts: " + ", ".join(f"{k}={v}" for k, v in flags_total.items())]
    if sess:
        stt = sess.get("stt_stats", {})
        out.append("stt: " + ", ".join(f"{k}={stt[k]}" for k in sorted(stt)))
        tts = {k: v for k, v in (sess.get("tts") or {}).items() if v is not None}
        out.append("tts: " + ", ".join(f"{k}={v}" for k, v in tts.items()))
        out.append(f"transcripts dropped={sess.get('transcripts_dropped')}  short speech "
                   f"ignored (ms)={sess.get('ignored_short_speech_ms')}  speech lost="
                   f"{sess.get('stt_gave_up')}  report={sess.get('report_written')}")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mockboard.voice.summarize")
    p.add_argument("path", nargs="?", help="a trace file (default: newest voice-*.jsonl)")
    p.add_argument("--trace-dir", default="traces")
    p.add_argument("--all", action="store_true", help="every voice session in the trace dir")
    a = p.parse_args(argv)
    if a.path:
        paths = [Path(a.path)]
    else:
        found = sorted(Path(a.trace_dir).glob("voice-*.jsonl"), key=lambda x: x.stat().st_mtime)
        if not found:
            print(f"No voice traces in {a.trace_dir}/. Run a voice session first.",
                  file=sys.stderr)
            return 1
        paths = found if a.all else found[-1:]
    for path in paths:
        print(f"# {path}")
        print("\n".join(summarize(load(path))))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
