"""Request handling by language: the board's rules (frozen detector + Tamil layer), labelled set.

Free and offline: no model, no speech service. The listener's vote is not included, so this is the
deterministic floor, the part that must work when the model is slow or down.

Honesty: every line is synthetic and written by us. The Tamil and Tanglish rows were written in the
same session as the Tamil phrase list, so they are design-time checks, not a holdout. None of this
says anything about real Prisma transcripts. Run from the repo root inside .venv:
    python -m eval.requests.runner
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from mockboard.brain.requests import detect_request
from mockboard.brain.requests_ta import detect_tamil_request

DATA = Path(__file__).with_name("multilingual.jsonl")


def classify(text: str) -> str:
    hit = detect_request(text) or detect_tamil_request(text)
    return hit.kind.value if hit else "none"


def main() -> int:
    rows = [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line]
    by_lang: dict[str, Counter] = defaultdict(Counter)
    misses: list[str] = []
    for row in rows:
        got = classify(row["text"])
        ok = got == row["expect"]
        lang = row["lang"]
        if row["expect"] == "none":
            by_lang[lang]["neg"] += 1
            by_lang[lang]["false_action"] += int(got != "none")
        else:
            by_lang[lang]["pos"] += 1
            by_lang[lang]["hit"] += int(ok)
        if not ok:
            misses.append(f"  {row['id']:<7} {row['group']:<11} expect={row['expect']:<16} "
                          f"got={got:<16} {row['text']}")
    print("Request rules by language. Lines are ours, plus 8 Prisma transcripts of GENERATED")
    print("speech (pg-*). No human speech. Tamil rows are not a holdout.")
    print(f"{'lang':<10} {'recall':>12} {'false actions':>15}")
    total = Counter()
    for lang, c in sorted(by_lang.items()):
        total.update(c)
        recall = f"{c['hit']}/{c['pos']}" if c["pos"] else "-"
        false = f"{c['false_action']}/{c['neg']}" if c["neg"] else "-"
        print(f"{lang:<10} {recall:>12} {false:>15}")
    recall_all = f"{total['hit']}/{total['pos']}"
    false_all = f"{total['false_action']}/{total['neg']}"
    print(f"{'all':<10} {recall_all:>12} {false_all:>15}")
    if misses:
        print("Misses (each is a known gap, not hidden):")
        print("\n".join(misses))
    # Gate: no false action on any negative or adversarial row. Recall gaps are reported, not
    # gated, because the detector is frozen until real transcripts exist (Ruling 57).
    return 1 if total["false_action"] else 0


if __name__ == "__main__":
    sys.exit(main())
