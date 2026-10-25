"""Eval harness: schema, loader/splits, metrics (incl. exact Clopper-Pearson), runner."""

from __future__ import annotations

import json

import pytest
from eval import runner
from eval.loader import load_items, select
from eval.metrics import (
    Counts,
    claims_match,
    clopper_pearson_upper,
    match_claims,
    summarize,
)
from eval.schema import EvalItem, ExpectedClaim, ItemClass, MoveClass, Split
from pydantic import ValidationError

from conftest import make_claim
from mockboard.brain.schemas import ClaimKind

EXAMPLES = runner.EXAMPLES


def test_examples_load_and_are_marked():
    items = load_items([EXAMPLES])
    assert len(items) == 6 and all(i.example for i in items)
    by_class = {c: sum(i.item_class == c for i in items) for c in ItemClass}
    assert by_class == {ItemClass.planted_error: 2, ItemClass.one_sided: 2, ItemClass.balanced: 2}
    assert {i.split for i in items} == {Split.dev, Split.heldout}


def test_heldout_is_guarded():
    items = load_items([EXAMPLES])
    with pytest.raises(PermissionError):
        select(items, "heldout")
    assert len(select(items, "heldout", allow_heldout=True)) == 3
    assert select(items, "dev", include_examples=False) == []


def test_schema_rules():
    base = {"id": "x", "split": "dev", "topic": "t", "question": "q", "answer": "a"}
    with pytest.raises(ValidationError):  # planted item without a planted claim
        EvalItem(item_class="planted_error", **base)
    with pytest.raises(ValidationError):  # checked claim without card id
        ExpectedClaim(claim_en="c", kind="numeric", value=1, label="true")
    item = EvalItem(item_class="one_sided", **base)
    assert item.expected_move_class == MoveClass.push_back


def test_loader_rejects_duplicates(tmp_path):
    line = json.dumps(
        {
            "id": "a",
            "split": "dev",
            "item_class": "balanced",
            "topic": "t",
            "question": "q",
            "answer": "a",
        }
    )
    path = tmp_path / "x.jsonl"
    path.write_text(line + "\n" + line + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_items([path])


@pytest.mark.parametrize(
    "k, n, expected",
    [  # reference values: scipy.stats.beta.ppf(0.975, k + 1, n - k), checked Oct 25, 2026
        (0, 150, 0.0242926),
        (2, 150, 0.0473330),
        (5, 30, 0.3472117),
        (3, 10, 0.6524529),
    ],
)
def test_clopper_pearson_upper_matches_reference(k, n, expected):
    assert clopper_pearson_upper(k, n) == pytest.approx(expected, abs=1e-6)


def test_clopper_pearson_edges():
    assert clopper_pearson_upper(4, 4) == 1.0
    assert clopper_pearson_upper(0, 150, two_sided=False) == pytest.approx(0.0197734, abs=1e-6)
    with pytest.raises(ValueError):
        clopper_pearson_upper(1, 0)


def test_claim_matching():
    pred = make_claim(value=1_500_000, unit="visitors", claim_en="arrivals")
    exp = ExpectedClaim(claim_en="arrivals", kind="numeric", value=15, unit="lakh visitors")
    assert claims_match(pred, exp)
    assert not claims_match(
        make_claim(value=30, unit="percent"),
        ExpectedClaim(claim_en="x", kind="numeric", value=30, unit="percentage points"),
    )
    date = make_claim(kind=ClaimKind.date, value=2018, unit="year")
    assert claims_match(date, ExpectedClaim(claim_en="x", kind="date", value=2018))
    a, b = make_claim("t0.s0.c0", value=9), make_claim("t0.s0.c1", value=9)
    exp9 = ExpectedClaim(claim_en="x", kind="numeric", value=9, unit="percent")
    assert match_claims([a, b], [exp9]) == [(0, 0)]  # one-to-one


def test_summarize_counts_and_f1():
    s = summarize(
        Counts(
            items=2,
            planted_total=2,
            planted_caught=1,
            true_claims_total=150,
            false_corrections=0,
            move_correct=1,
            claims_pred=4,
            claims_expected=2,
            claims_matched=2,
        )
    )
    assert s["planted_error_recall"] == 0.5
    assert s["false_correction_cp_upper"] == pytest.approx(0.0242926, abs=1e-6)
    assert s["claim_f1"] == pytest.approx(2 * 0.5 * 1.0 / 1.5)
    assert summarize(Counts())["claim_f1"] is None


async def test_runner_on_examples_with_fake_llm(settings):
    import argparse

    args = argparse.Namespace(
        items=[str(EXAMPLES)],
        split="dev",
        allow_heldout=False,
        cards="builtin",
        real=False,
        no_examples=False,
        no_write=True,
    )
    out = await runner.main_async(args)
    c = out["summary"]["counts"]
    assert out["llm"] == "fake-llm" and out["n_items"] == 3 and out["includes_examples"]
    assert c["planted_caught"] == c["planted_total"] == 1
    assert c["false_corrections"] == 0
    assert c["move_correct"] == 3
    assert "Clopper-Pearson" in out["summary"]["false_correction_cp_note"]


async def test_runner_auto_cards_falls_back_to_builtin(settings):
    import argparse

    args = argparse.Namespace(
        items=[str(EXAMPLES)],
        split="dev",
        allow_heldout=False,
        cards="auto",
        real=False,
        no_examples=False,
        no_write=True,
    )
    out = await runner.main_async(args)
    assert out["cards"]["origin"].startswith("builtin")
