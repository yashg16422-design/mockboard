"""Load eval items from JSONL files and select a split."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from eval.schema import EvalItem, Split

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def load_items(paths: Iterable[Path] | None = None) -> list[EvalItem]:
    """Read every `*.jsonl` in eval/golden/ (or the given files). Blank and `//` lines skip.

    Raises on a malformed item or a duplicate id, so a broken set fails loudly.
    """
    files = sorted(paths) if paths is not None else sorted(GOLDEN_DIR.glob("*.jsonl"))
    items: list[EvalItem] = []
    seen: set[str] = set()
    for path in files:
        for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("//"):
                continue
            try:
                item = EvalItem.model_validate(json.loads(line))
            except Exception as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc
            if item.id in seen:
                raise ValueError(f"{path}:{lineno}: duplicate item id {item.id!r}")
            seen.add(item.id)
            items.append(item)
    return items


def select(
    items: Iterable[EvalItem],
    split: Split | str,
    *,
    include_examples: bool = True,
    allow_heldout: bool = False,
) -> list[EvalItem]:
    """Items of one split. The held-out split needs `allow_heldout=True` (milestones only)."""
    split = Split(split)
    if split == Split.heldout and not allow_heldout:
        raise PermissionError("the held-out split runs only at milestones; pass allow_heldout")
    return [i for i in items if i.split == split and (include_examples or not i.example)]
