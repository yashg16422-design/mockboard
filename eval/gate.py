"""The eval merge gate (ENGINEERING.md §5): run the eval and exit non-zero on a regression.

    python -m eval.gate                                  # dev split, thresholds from settings
    python -m eval.gate --baseline eval/baselines/dev.json   # also compare with a baseline run

A run FAILS when any of these is true:

1. planted-error recall is below `EVAL_GATE_MIN_PLANTED_RECALL` (default 0.8);
2. false corrections exceed `EVAL_GATE_MAX_FALSE_CORRECTIONS` (default 0: any false
   correction fails, because the board challenging a true statement is the worst error);
3. the items contain no planted error at all (a gate that measures nothing must not pass);
4. with `--baseline`: fewer planted errors caught, or more false corrections, than the
   baseline run's counts (counts, not percentages, because each item in a small set is worth
   several percentage points).

The held-out split is refused unless `--allow-heldout` is given, exactly as in the runner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from eval import runner
from mockboard.config import get_settings


def gate_failures(
    out: dict[str, Any],
    *,
    min_planted_recall: float,
    max_false_corrections: int,
    baseline: dict[str, Any] | None = None,
) -> list[str]:
    """Why this eval run fails the gate, as plain sentences (an empty list means it passes)."""
    counts = out["summary"]["counts"]
    caught, planted = counts["planted_caught"], counts["planted_total"]
    false_corr = counts["false_corrections"]
    failures: list[str] = []
    if planted == 0:
        failures.append("The items contain no planted errors, so recall cannot be measured.")
    elif caught / planted < min_planted_recall:
        failures.append(
            f"Planted-error recall is {caught}/{planted} ({caught / planted:.0%}), below the "
            f"threshold of {min_planted_recall:.0%}."
        )
    if false_corr > max_false_corrections:
        failures.append(
            f"There were {false_corr} false correction(s) on true claims; at most "
            f"{max_false_corrections} allowed."
        )
    if baseline is not None:
        base = baseline["summary"]["counts"]
        if caught < base["planted_caught"]:
            failures.append(
                f"Planted errors caught went down from {base['planted_caught']} (baseline) "
                f"to {caught}."
            )
        if false_corr > base["false_corrections"]:
            failures.append(
                f"False corrections went up from {base['false_corrections']} (baseline) "
                f"to {false_corr}."
            )
    return failures


def gate_notes(out: dict[str, Any]) -> list[str]:
    """Warnings that do not fail the gate but make its result weaker."""
    counts = out["summary"]["counts"]
    notes = []
    if counts["true_claims_total"] == 0:
        notes.append("No true claims were extracted, so the false-correction check is vacuous.")
    if out.get("includes_examples"):
        notes.append("The items include illustrative EXAMPLE items, not the frozen golden set.")
    return notes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eval.gate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--items", nargs="*", help="JSONL files (default: as eval.runner)")
    p.add_argument("--split", default="dev", choices=["dev", "heldout"])
    p.add_argument("--allow-heldout", action="store_true", help="milestone runs only")
    p.add_argument("--cards", default="auto", choices=["auto", "data", "builtin"])
    p.add_argument("--real", action="store_true", help="use Nova Pro (costs money)")
    p.add_argument("--baseline", help="a committed eval summary JSON to compare counts against")
    p.add_argument("--no-write", action="store_true", help="do not write a results file")
    return p.parse_args(argv)


def run_gate(args: argparse.Namespace) -> tuple[int, dict[str, Any], list[str]]:
    """Run the eval, apply the gate, and return (exit code, eval output, failures)."""
    runner_argv = ["--split", args.split, "--cards", args.cards]
    if args.items:
        runner_argv += ["--items", *args.items]
    for flag, on in (("--allow-heldout", args.allow_heldout), ("--real", args.real),
                     ("--no-write", args.no_write)):
        if on:
            runner_argv.append(flag)
    out = asyncio.run(runner.main_async(runner.parse_args(runner_argv)))
    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    s = get_settings()
    failures = gate_failures(
        out,
        min_planted_recall=s.eval_gate_min_planted_recall,
        max_false_corrections=s.eval_gate_max_false_corrections,
        baseline=baseline,
    )
    return (1 if failures else 0), out, failures


def main(argv: list[str] | None = None) -> int:
    code, out, failures = run_gate(parse_args(argv))
    s = get_settings()
    print(runner.format_summary(out))
    print(
        f"Eval gate: recall must be at least {s.eval_gate_min_planted_recall:.0%}, "
        f"false corrections at most {s.eval_gate_max_false_corrections}."
    )
    for note in gate_notes(out):
        print(f"  note: {note}")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("Eval gate FAILED.")
    else:
        print("Eval gate passed.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
