"""The debate scoreboard, computed by CODE from the debate record (CTO_NOTES_04 §4, DB6).

No model is involved. The input is one row per user turn: the `debate` block the session
writes on every trace line (`DebateSessionHooks.rows`), so the same scoreboard comes out of a
live session and out of its trace file (`rows_from_trace`). A test checks both agree.

Per round:
* `turns`: the user's turns in the round;
* `arguments_made`: claim-and-warrant pairs the listener found in the user's speech;
* `answered`: opponent arguments the user answered for the first time in this round;
* `standing_after`: opponent arguments still standing (not conceded) when the round ended;
* `concessions_won`: opponent arguments the opponent conceded in this round (rule D8);
* `figures_questioned`: the user's figures the board asked about (rule D9, fact cards only).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from mockboard.brain.schemas import DebateScoreboard, Move, RoundScore


def rows_from_trace(path: Path) -> list[dict[str, Any]]:
    """The per-turn `debate` blocks of a session's trace file, in order."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("event") is None and isinstance(row.get("debate"), dict):
            rows.append(row["debate"])
    return rows


def compute_scoreboard(
    rows: Sequence[Mapping[str, Any]], case_ids: Iterable[str]
) -> DebateScoreboard:
    case = list(case_ids)
    rounds: list[RoundScore] = []
    by_round: dict[str, dict[str, Any]] = {}
    answered_before: set[str] = set()
    conceded_before: set[str] = set()
    standing = len(case)
    for row in rows:
        name = str(row.get("round") or "")
        if name not in by_round:
            by_round[name] = {
                "round": name, "turns": 0, "arguments_made": 0, "answered": 0,
                "standing_after": standing, "concessions_won": 0, "figures_questioned": 0,
            }
        r = by_round[name]
        answered = set(row.get("answered_ids") or []) & set(case)
        conceded = set(row.get("conceded_ids") or []) & set(case)
        standing = len([i for i in row.get("standing_ids") or [] if i in case])
        r["turns"] += 1
        r["arguments_made"] += int(row.get("arguments_made") or 0)
        r["answered"] += len(answered - answered_before)
        r["concessions_won"] += len(conceded - conceded_before)
        r["figures_questioned"] += int(row.get("move") == Move.fact_challenge.value)
        r["standing_after"] = standing
        answered_before |= answered
        conceded_before |= conceded
    rounds = [RoundScore(**r) for r in by_round.values()]
    return DebateScoreboard(
        rounds=rounds,
        opponent_arguments=len(case),
        arguments_made=sum(r.arguments_made for r in rounds),
        answered=len(answered_before),
        standing_at_end=standing,
        concessions_won=len(conceded_before),
        figures_questioned=sum(r.figures_questioned for r in rounds),
    )
