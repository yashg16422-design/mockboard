"""Per-turn trace recorder (ENGINEERING.md §8).

One JSON line per turn in `<trace_dir>/<session_id>.jsonl`, appended and flushed as soon as
the turn ends, so an interrupted session keeps every finished turn. Stage times are
`time.monotonic()` values converted to milliseconds since the turn started; the wall-clock
time is recorded once per turn only for humans.

Text mode has no audio, so the voice stages (VAD end, Timbre first byte, playback start) are
absent rather than faked. Phase 2 adds them with the same `mark()` call.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mockboard.brain.llm import CallStats


@dataclass
class TurnTrace:
    session_id: str
    turn_id: str
    turn_index: int
    topic: str
    t0: float = field(default_factory=time.monotonic)
    wall_clock: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    stages: dict[str, float] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)

    def mark(self, stage: str, at: float | None = None) -> None:
        """Record a stage once (the first time it happens), as ms since turn start."""
        t = time.monotonic() if at is None else at
        self.stages.setdefault(stage, round((t - self.t0) * 1000, 2))

    def set(self, **values: Any) -> None:
        self.fields.update(values)


class TraceRecorder:
    """Collects model-call stats by trace tag and writes one line per turn."""

    def __init__(self, trace_dir: Path, session_id: str, *, enabled: bool = True) -> None:
        self.session_id = session_id
        self.enabled = enabled
        self.path = Path(trace_dir) / f"{session_id}.jsonl"
        self._calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def on_call(self, stats: CallStats) -> None:
        """LLM client sink: every call (listener, librarian, actor, judge) lands here."""
        self._calls[stats.tag or "_untagged"].append(stats.as_dict())

    def start_turn(self, turn_id: str, turn_index: int, topic: str) -> TurnTrace:
        return TurnTrace(self.session_id, turn_id, turn_index, topic)

    def calls_for(self, tag: str) -> list[dict[str, Any]]:
        return list(self._calls.get(tag, []))

    def pop_calls(self, tag: str) -> list[dict[str, Any]]:
        """Take (and forget) the calls recorded under `tag`, e.g. one fact-check run."""
        return self._calls.pop(tag, [])

    def merge_calls(self, from_tag: str, into_tag: str) -> None:
        """Move the calls recorded under `from_tag` so they are written with `into_tag`."""
        self._calls[into_tag].extend(self._calls.pop(from_tag, []))

    def write_turn(self, turn: TurnTrace) -> dict[str, Any]:
        calls = self._calls.pop(turn.turn_id, [])
        totals = {
            k: sum(int(c.get(k) or 0) for c in calls)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        }
        line = {
            "session_id": turn.session_id,
            "turn_id": turn.turn_id,
            "turn_index": turn.turn_index,
            "topic": turn.topic,
            "wall_clock": turn.wall_clock,
            "stages_ms": dict(sorted(turn.stages.items(), key=lambda kv: kv[1])),
            "llm_calls": calls,
            "tokens": totals,
            **turn.fields,
        }
        if self.enabled:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
                fh.flush()
        return line

    def write_event(self, kind: str, **values: Any) -> dict[str, Any]:
        """A non-turn line (session start, report). Same file, `event` field set."""
        tag = values.pop("tag", None)
        line = {
            "session_id": self.session_id,
            "event": kind,
            "wall_clock": datetime.now(UTC).isoformat(),
            **values,
        }
        if tag is not None:
            line["llm_calls"] = self._calls.pop(tag, [])
        if self.enabled:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        return line
