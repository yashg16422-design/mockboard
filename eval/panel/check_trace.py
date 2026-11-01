"""P11: check a panel session trace. No secrets, no invented facts.

A good trace has a session_start, every turn names its agenda item and member, a hand-off
when the sitting leaves its first segment, a refused topic does not return, counts stay
inside the cap, and a close is present. A one-segment sitting (an early end, a replay)
needs no hand-off.
A debate trace is skipped (exit 0) because this checker is for the panel.

Usage, from the repo folder, inside .venv:
  python -m eval.panel.check_trace traces/<session>.jsonl
  make check-trace TRACE=traces/<session>.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["check_trace", "main"]


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def check_trace(path: Path) -> list[str]:
    """Problems with this trace. Empty = pass."""
    if not path.is_file():
        return [f"no such file: {path}"]
    try:
        rows = _rows(path)
    except json.JSONDecodeError as exc:
        return [f"not JSONL: {exc}"]
    if not rows:
        return ["the file is empty"]
    start = next((r for r in rows if r.get("event") == "session_start"), None)
    if start is None:
        return ["no session_start"]
    if start.get("mode") == "debate" or (start.get("prompt_versions") or {}).get("listener_debate"):
        return []  # debate is out of this checker's scope
    turns = [r for r in rows if r.get("turn_id") and r.get("event") is None]
    if not turns:
        return ["no turns"]
    problems: list[str] = []
    handoffs = 0
    segments: set[str] = set()
    closed = False
    avoid: list[str] = []
    answers = 0
    cap = None
    for row in turns:
        panel = row.get("panel") or {}
        if not panel.get("segment_id"):
            problems.append(f"{row['turn_id']}: no agenda item")
        if not panel.get("member"):
            problems.append(f"{row['turn_id']}: no member")
        segment_id = str(panel.get("segment_id") or "")
        if segment_id:
            segments.add(segment_id)
        if panel.get("route") == "handoff":
            handoffs += 1
        if panel.get("route") == "close" or panel.get("close_reason"):
            closed = True
        if panel.get("counted"):
            answers += 1
        if panel.get("max_answers") is not None:
            cap = panel["max_answers"]
        if panel.get("candidate_request") == "change_topic":
            avoid = list(panel.get("avoid_terms") or [])
        elif avoid:
            blob = " ".join(
                part.get("text", "") for part in panel.get("parts") or []
            ).lower()
            blob += " " + str(row.get("line") or row.get("text") or "").lower()
            for term in avoid:
                token = str(term).strip().lower()
                if token and token in blob.split():
                    problems.append(
                        f"{row['turn_id']}: refused term {term!r} returned after a topic change"
                    )
    if len(segments) > 1 and handoffs < 1:
        problems.append("no spoken hand-off")
    if not closed and not any(r.get("event") == "report" for r in rows):
        problems.append("no close and no report")
    if cap is not None and answers > int(cap):
        problems.append(f"counted answers {answers} over the cap {cap}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("trace", type=Path, help="a session jsonl under traces/")
    args = p.parse_args(argv)
    problems = check_trace(args.trace)
    if problems:
        print(f"FAIL {args.trace}")
        for item in problems:
            print(f"  - {item}")
        return 1
    print(f"PASS {args.trace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
