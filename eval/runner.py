"""Run the text eval set through the REAL turn-analysis graph (FakeLLMClient by default).

Per item:
1. Turn 0: the scripted answer goes through listener -> librarian -> gate -> strategist.
2. Turn 1: a neutral, balanced, claim-free follow-up answer, so a delayed fact challenge
   (which never fires in the same turn) has its chance. The predicted move class is
   "challenge" if turn 1 challenges a turn-0 claim, else the class of turn 0's move.

    python -m eval.runner --items eval/examples/examples.jsonl --cards builtin --split dev
    python -m eval.runner --split heldout --allow-heldout          # milestones only
    python -m eval.runner --real                                    # Nova Pro (costs money)

The fake LLM is a rule-based stand-in: its numbers test the PIPELINE, never Nova's quality.
Each run writes a summary JSON to eval/results/ (git-ignored except committed summaries).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.loader import GOLDEN_DIR, load_items, select
from eval.metrics import MOVE_TO_CLASS, Counts, match_claims, summarize
from eval.schema import ClaimLabel, EvalItem, ItemClass, MoveClass, Split
from mockboard.brain.cards import CardSet, builtin_test_cards, load_cards, load_cards_or_builtin
from mockboard.brain.deps import BrainDeps, build_deps
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import BedrockNovaClient, CallStats
from mockboard.brain.numbers import Tolerance
from mockboard.brain.schemas import Move, Segment, StrategyContext, Verdict
from mockboard.config import REPO_ROOT, Settings, get_settings

EXAMPLES = Path(__file__).resolve().parent / "examples" / "examples.jsonl"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
NEUTRAL_FOLLOW_UP = (
    "Is par do pehlu hain. Ek taraf rozgaar aur vikas hai, lekin doosri taraf "
    "paryavaran aur sthaniya logon ki chintayein bhi hain, isliye santulan zaroori hai."
)


@dataclass
class ItemResult:
    item_id: str
    item_class: str
    expected_move_class: str
    predicted_move_class: str
    moves: list[str]
    claims: list[dict[str, Any]]
    counts: Counts


def pick_cards(
    mode: str, settings: Settings, items: list[EvalItem] | None = None
) -> tuple[CardSet, str]:
    """`auto` uses data/cards when it covers every card id the items cite, else the built-in
    synthetic set if that covers them (the shipped examples cite built-in cards)."""
    if mode == "builtin":
        return builtin_test_cards(settings.topics), "builtin-test-cards"
    if mode == "data":
        return load_cards(settings.cards_dir, settings.topics), f"data:{settings.cards_dir}"
    cards, origin = load_cards_or_builtin(settings.cards_dir, settings.topics)
    cited = {c.card_id for i in items or [] for c in i.expected_claims if c.card_id}
    if cited - set(cards.cards):
        builtin = builtin_test_cards(settings.topics)
        if not cited - set(builtin.cards):
            return builtin, "builtin-test-cards (auto: items cite built-in cards)"
        raise ValueError(f"items cite unknown card ids: {sorted(cited - set(cards.cards))}")
    return cards, origin


async def run_item(analyzer: TurnAnalyzer, item: EvalItem) -> ItemResult:
    sid = f"eval-{item.id}"
    moves = []
    result = None
    for i, (text, question) in enumerate(
        [(item.answer, item.question), (NEUTRAL_FOLLOW_UP, "follow-up")]
    ):
        ctx = StrategyContext(
            turn_id=f"t{i}",
            turn_index=i,
            topic=item.topic,
            turns_in_topic=i,
            max_turns_in_topic=10,
            next_topic=None,
        )
        result = await analyzer.analyse(
            session_id=sid,
            turn_id=f"t{i}",
            turn_index=i,
            question=question,
            segment=Segment(segment_id="s0", segment_index=0, text=text),
            ctx=ctx,
        )
        moves.append(result.move)
    await analyzer.close_session(sid)
    assert result is not None
    ledger = [c for c in result.ledger if c.turn_id == "t0"]

    challenged_t0 = moves[1].move == Move.fact_challenge and (moves[1].target or "").startswith(
        "t0."
    )
    predicted = MoveClass.challenge if challenged_t0 else MOVE_TO_CLASS[moves[0].move]

    c = Counts(items=1, fallbacks=len(result.fallbacks))
    c.move_correct = int(predicted == item.expected_move_class)
    pairs = match_claims(ledger, item.expected_claims)
    c.claims_pred, c.claims_expected, c.claims_matched = (
        len(ledger),
        len(item.expected_claims),
        len(pairs),
    )
    matched_exp = {j: ledger[i] for i, j in pairs}
    for j, exp in enumerate(item.expected_claims):
        pred = matched_exp.get(j)
        if exp.label == ClaimLabel.planted_error:
            c.planted_total += 1
            c.planted_caught += int(
                pred is not None
                and pred.verdict == Verdict.contradicted
                and pred.card_id == exp.card_id
            )
        elif exp.label == ClaimLabel.true:
            if pred is None:
                c.true_claims_missed += 1
            else:
                c.true_claims_total += 1
                c.false_corrections += int(pred.verdict == Verdict.contradicted)
    if item.item_class == ItemClass.balanced:
        c.balanced_items = 1
        c.balanced_pushback = int(predicted == MoveClass.push_back)
    return ItemResult(
        item_id=item.id,
        item_class=item.item_class.value,
        expected_move_class=str(item.expected_move_class),
        predicted_move_class=predicted.value,
        moves=[m.move.value for m in moves],
        claims=[
            {
                "id": x.id,
                "verdict": x.verdict.value,
                "card_id": x.card_id,
                "numeric_check": x.numeric_check,
                "value": x.value,
                "unit": x.unit,
            }
            for x in ledger
        ],
        counts=c,
    )


async def run_eval(items: list[EvalItem], deps: BrainDeps) -> tuple[Counts, list[ItemResult]]:
    analyzer = TurnAnalyzer(deps)
    total = Counts()
    results = []
    for item in items:
        r = await run_item(analyzer, item)
        total.add(r.counts)
        results.append(r)
    return total, results


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build_eval_deps(settings: Settings, cards: CardSet, real: bool, sink) -> BrainDeps:
    if real:
        llm = BedrockNovaClient.from_settings(settings, on_call=sink)
    else:
        tol = Tolerance(
            rel=settings.numeric_rel_tolerance, abs_pp=settings.numeric_abs_tolerance_pp
        )
        llm = make_fake_llm(cards, tol, on_call=sink)
    return build_deps(settings, llm, cards=cards)


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    settings = settings.model_copy(update={"graph_retry_initial_interval_s": 0.05})
    if args.items:
        paths = [Path(p) for p in args.items]
    elif any(GOLDEN_DIR.glob("*.jsonl")):
        paths = None
    else:
        paths = [EXAMPLES]
    items = select(
        load_items(paths),
        args.split,
        include_examples=not args.no_examples,
        allow_heldout=args.allow_heldout,
    )
    cards, origin = pick_cards(args.cards, settings, items)
    calls: list[CallStats] = []
    deps = build_eval_deps(settings, cards, args.real, calls.append)
    deps = replace(deps, settings=settings)
    total, results = await run_eval(items, deps)
    summary = summarize(total)
    out = {
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(),
        "llm": deps.llm.model_id,
        "split": Split(args.split).value,
        "items_files": [str(p) for p in (paths or sorted(GOLDEN_DIR.glob("*.jsonl")))],
        "n_items": len(items),
        "includes_examples": any(i.example for i in items),
        "cards": {
            "origin": origin,
            "version": cards.version,
            "count": len(cards.cards),
            "all_synthetic": cards.all_synthetic,
        },
        "prompt_versions": settings.prompt_versions(),
        "feature_flags": settings.feature_flags(),
        "tokens": {
            k: sum(getattr(c, k) for c in calls)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        },
        "llm_calls": len(calls),
        "summary": summary,
        "items": [{**r.__dict__, "counts": r.counts.__dict__} for r in results],
    }
    if not args.no_write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = RESULTS_DIR / f"eval-{out['split']}-{stamp}.json"
        path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        out["written_to"] = str(path)
    return out


def format_summary(out: dict[str, Any]) -> str:
    s = out["summary"]
    c = s["counts"]

    def pct(v):
        return "n/a" if v is None else f"{v:.0%}"

    f1_text = "n/a" if s["claim_f1"] is None else f"{s['claim_f1']:.2f}"
    lines = [
        f"Eval split={out['split']} items={out['n_items']} llm={out['llm']} "
        f"cards={out['cards']['origin']} (synthetic={out['cards']['all_synthetic']})",
        f"  planted errors caught : {c['planted_caught']}/{c['planted_total']} "
        f"({pct(s['planted_error_recall'])})",
        f"  false corrections     : {c['false_corrections']}/{c['true_claims_total']} "
        f"(95% CP upper {pct(s['false_correction_cp_upper'])}); true claims not extracted: "
        f"{c['true_claims_missed']}",
        f"  move-class accuracy   : {c['move_correct']}/{c['items']} "
        f"({pct(s['move_class_accuracy'])})",
        f"  balanced pushback     : {c['balanced_pushback']}/{c['balanced_items']}",
        f"  claim F1              : {f1_text}"
        f" (matched {c['claims_matched']}, predicted {c['claims_pred']}, "
        f"expected {c['claims_expected']})",
        f"  fallbacks             : {c['fallbacks']}",
    ]
    if out["includes_examples"]:
        lines.append("  NOTE: includes illustrative EXAMPLE items, not the frozen golden set.")
    if out["llm"] == "fake-llm":
        lines.append("  NOTE: fake rule-based LLM; these numbers test the pipeline, not Nova.")
    if "written_to" in out:
        lines.append(f"  summary written to {out['written_to']}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="eval.runner",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--items",
        nargs="*",
        help="JSONL files (default: eval/golden/*.jsonl, else eval/examples/examples.jsonl)",
    )
    p.add_argument("--split", default="dev", choices=[s.value for s in Split])
    p.add_argument("--allow-heldout", action="store_true", help="milestone runs only")
    p.add_argument("--cards", default="auto", choices=["auto", "data", "builtin"])
    p.add_argument("--real", action="store_true", help="use Nova Pro (costs money)")
    p.add_argument("--no-examples", action="store_true", help="skip items marked example")
    p.add_argument("--no-write", action="store_true", help="do not write a results file")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    out = asyncio.run(main_async(parse_args(argv)))
    print(format_summary(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
