"""Run the labelled custom-motion set through the safety screen.

    python -m eval.motions.runner            # rules + fake classifier (offline)
    python -m eval.motions.runner --real     # rules + real Nova classifier (costs money)

The gate (CTO_NOTES_04 §5): ZERO partisan motions may pass (allowed or reframed). The run
exits non-zero if one does.

Every decision is tagged with the layer that made it. A decision made by the classifier layer
in a FAKE run came from `fake_responders.motion_screen_responder`, a word-list stand-in, so it
is marked `needs_real_nova`: only a `--real` run says how Nova itself screens it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from mockboard.brain.cards import builtin_test_cards
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.llm import BedrockNovaClient, LLMClient
from mockboard.config import get_settings
from mockboard.modes.debate.screen import ScreenDecision, ScreenLayer, screen_motion

ITEMS = Path(__file__).resolve().parent / "motions.jsonl"


@dataclass
class MotionResult:
    id: str
    motion: str
    expected: str
    got: str
    layer: str
    category: str
    partisan: bool
    correct: bool
    needs_real_nova: bool
    reframed: str | None


def load_items(path: Path = ITEMS) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]


async def run_items(llm: LLMClient, items: list[dict], *, real: bool) -> list[MotionResult]:
    out: list[MotionResult] = []
    for item in items:
        res = await screen_motion(item["motion"], llm=llm, enabled=True)
        out.append(
            MotionResult(
                id=item["id"],
                motion=res.motion,
                expected=item["expected"],
                got=res.decision.value,
                layer=res.layer.value,
                category=res.category,
                partisan=bool(item.get("partisan")),
                correct=res.decision.value == item["expected"],
                needs_real_nova=(not real) and res.layer == ScreenLayer.classifier,
                reframed=res.reframed,
            )
        )
    return out


def summarize(results: list[MotionResult]) -> dict:
    partisan_passed = [
        r.id for r in results if r.partisan and r.got != ScreenDecision.refuse.value
    ]
    by_layer: dict[str, int] = {}
    for r in results:
        by_layer[r.layer] = by_layer.get(r.layer, 0) + 1
    return {
        "items": len(results),
        "correct": sum(r.correct for r in results),
        "partisan_items": sum(r.partisan for r in results),
        "partisan_passed": partisan_passed,
        "unsafe_allowed": [
            r.id for r in results if r.expected == "refuse" and r.got != "refuse"
        ],
        "safe_refused": [r.id for r in results if r.expected != "refuse" and r.got == "refuse"],
        "by_layer": by_layer,
        "needs_real_nova": [r.id for r in results if r.needs_real_nova],
        "gate_passed": not partisan_passed,
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
        print(json.dumps([asdict(r) for r in results], indent=1, ensure_ascii=False))
    for r in results:
        flag = "" if r.correct else "  <-- MISMATCH"
        nova = "  (needs real Nova)" if r.needs_real_nova else ""
        print(f"{r.id} {r.expected:>7} -> {r.got:<7} [{r.layer}/{r.category}]{flag}{nova}")
    print(json.dumps(summary, indent=1))
    return 0 if summary["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
