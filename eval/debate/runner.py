"""Run the scripted debate set and check every invariant and the report.

    python -m eval.debate.runner            # offline fake LLM (rule-based stand-ins)
    python -m eval.debate.runner --real     # real Nova on Bedrock (costs money)

The gate (exit code 1 when it fails): ZERO invariant violations (`invariants.py`, CTO_NOTES_04
§7) and ZERO report problems (quotes are the user's own words, the scoreboard matches the
debate record, every verdict reason traces to a rubric score or the scoreboard, the verdict
never takes a side). `must_include` (moves a working listener should trigger) is gated in fake
runs; in a `--real` run it is reported, because Nova's listener may read a turn differently.

Both modes use the built-in synthetic test cards ("Testland"), so the planted figure in d07
has a card to be checked against. Nothing here is a real fact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eval.debate.invariants import make_setup, play_debate, report_problems, violations
from mockboard.brain.cards import builtin_test_cards
from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.llm import BedrockNovaClient, LLMClient
from mockboard.config import get_settings

ITEMS = Path(__file__).resolve().parent / "debates.jsonl"


def load_items(path: Path = ITEMS) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]


async def run_items(llm: LLMClient, items: list[dict], *, real: bool) -> list[dict]:
    settings = get_settings()
    # Test cards on purpose, also with real Nova: d07 checks a planted figure (ruling 36).
    deps = build_deps(settings, llm, cards=builtin_test_cards(), allow_synthetic_cards=True)
    out = []
    for item in items:
        setup = make_setup(item["motion"], item.get("side", "for"), item.get("length", "quick"),
                           item.get("style", "logician"), item.get("language", "hinglish"))
        p = await play_debate(deps, setup, item["answers"], f"eval-{item['id']}", report=True,
                              language=item.get("report_language", "en"))
        moves = [m.move.value for m in p.moves]
        missing = [m for m in item.get("must_include", []) if m not in moves]
        rep = p.report
        out.append({
            "id": item["id"],
            "motion": item["motion"],
            "moves": [f"{m.move.value}/{m.rule_id}" for m in p.moves],
            "violations": violations(p),
            "report_problems": report_problems(p),
            "missing_moves": missing,
            "gated_missing": [] if real else missing,
            "verdict": rep.verdict.headline,
            "best_line": rep.best_line.quote if rep.best_line else None,
            "scoreboard": rep.scoreboard.model_dump(),
            "report_language": rep.report_language.value,
            "judge_fallback": rep.is_fallback,
        })
    return out


def summarize(results: list[dict]) -> dict:
    failed = [r["id"] for r in results
              if r["violations"] or r["report_problems"] or r["gated_missing"]]
    counts: dict[str, int] = {}
    for r in results:
        for name, v in r["violations"].items():
            counts[name] = counts.get(name, 0) + len(v)
    return {
        "items": len(results),
        "failed": failed,
        "invariant_violations": counts,
        "report_problems": sum(len(r["report_problems"]) for r in results),
        "missing_moves_reported": {r["id"]: r["missing_moves"] for r in results
                                   if r["missing_moves"]},
        "judge_fallbacks": sum(r["judge_fallback"] for r in results),
        "gate_passed": not failed,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--real", action="store_true", help="use Nova on Bedrock (costs money)")
    p.add_argument("--items", type=Path, default=ITEMS)
    p.add_argument("--json", action="store_true", help="print per-item results as JSON")
    args = p.parse_args(argv)
    llm: LLMClient
    if args.real:
        llm = BedrockNovaClient.from_settings(get_settings())
    else:
        llm = make_fake_llm(builtin_test_cards())
    results = asyncio.run(run_items(llm, load_items(args.items), real=args.real))
    summary = summarize(results)
    if args.json:
        print(json.dumps(results, indent=1, ensure_ascii=False))
    for r in results:
        ok = not (r["violations"] or r["report_problems"] or r["gated_missing"])
        print(f"{r['id']} {r['motion']:<24} {'ok  ' if ok else 'FAIL'} verdict: {r['verdict']}")
        print(f"    moves: {' '.join(r['moves'])}")
        for name, v in r["violations"].items():
            print(f"    invariant {name}: {v}")
        for problem in r["report_problems"]:
            print(f"    report: {problem}")
        if r["missing_moves"]:
            print(f"    missing moves: {r['missing_moves']}"
                  + ("" if r["gated_missing"] else " (reported, not gated)"))
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0 if summary["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
