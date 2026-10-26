"""`make eval-gate` (ENGINEERING.md §5): the eval exits non-zero when planted-error recall drops
below the threshold in settings, when any false correction appears, or (with a baseline) when
the counts regress. Thresholds come from settings."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from eval import gate

from mockboard.config import REPO_ROOT, get_settings

ANSWER = (
    "Testland mein tourism share of GDP {v} percent hai in 2023, "
    "isliye ise priority milni chahiye."
)
CARD = "builtin_tourism_gdp_share"  # the built-in synthetic card says 9 percent in 2023


def item(item_id: str, item_class: str, value: int, label: str) -> dict:
    return {
        "id": item_id,
        "split": "dev",
        "item_class": item_class,
        "topic": "tourism",
        "question": "Testland ki economy mein tourism kitna important hai?",
        "answer": ANSWER.format(v=value),
        "expected_claims": [
            {
                "claim_en": f"Testland tourism share of GDP is {value} percent in 2023",
                "kind": "numeric",
                "value": value,
                "unit": "percent",
                "year": 2023,
                "label": label,
                "card_id": CARD,
            }
        ],
    }


def write_items(tmp_path, *items: dict) -> str:
    path = tmp_path / "items.jsonl"
    path.write_text("\n".join(json.dumps(i) for i in items), encoding="utf-8")
    return str(path)


def summary(caught: int, planted: int, false_corr: int, true_total: int = 10) -> dict:
    return {
        "summary": {
            "counts": {
                "planted_caught": caught,
                "planted_total": planted,
                "false_corrections": false_corr,
                "true_claims_total": true_total,
            }
        }
    }


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_good_run_passes():
    assert gate.gate_failures(summary(9, 10, 0), min_planted_recall=0.8,
                              max_false_corrections=0) == []


@pytest.mark.parametrize(
    "run, problem",
    [
        (summary(7, 10, 0), "below the threshold"),
        (summary(10, 10, 1), "false correction"),
        (summary(0, 0, 0), "no planted errors"),
    ],
    ids=["low_recall", "any_false_correction", "nothing_measured"],
)
def test_a_bad_run_fails_with_a_reason(run, problem):
    failures = gate.gate_failures(run, min_planted_recall=0.8, max_false_corrections=0)
    assert len(failures) == 1 and problem in failures[0]


def test_counts_must_not_regress_against_a_baseline():
    base = summary(9, 10, 0)
    assert gate.gate_failures(summary(9, 10, 0), min_planted_recall=0.5,
                              max_false_corrections=2, baseline=base) == []
    failures = gate.gate_failures(summary(8, 10, 1), min_planted_recall=0.5,
                                  max_false_corrections=2, baseline=base)
    assert any("went down from 9" in f for f in failures)
    assert any("went up from 0" in f for f in failures)


def test_the_shipped_examples_pass_the_gate():
    code, out, failures = gate.run_gate(gate.parse_args(["--no-write"]))
    assert (code, failures) == (0, [])
    assert out["split"] == "dev"


def test_a_false_correction_fails_the_real_run(tmp_path):
    # A planted error the fake catches, plus a claim labelled "true" that contradicts the
    # card: the board would challenge a statement the owner marked as correct.
    items = write_items(
        tmp_path,
        item("planted", "planted_error", 25, "planted_error"),
        item("true-but-wrong", "balanced", 30, "true"),
    )
    code, out, failures = gate.run_gate(
        gate.parse_args(["--items", items, "--cards", "builtin", "--no-write"])
    )
    assert code == 1 and out["summary"]["counts"]["false_corrections"] == 1
    assert any("false correction" in f for f in failures)


def test_the_recall_threshold_comes_from_settings(tmp_path, monkeypatch):
    # One planted error that matches the card (9 percent), so the board cannot catch it,
    # and one it does catch: recall is 50 percent.
    items = write_items(
        tmp_path,
        item("caught", "planted_error", 25, "planted_error"),
        item("missed", "planted_error", 9, "planted_error"),
    )
    args = gate.parse_args(["--items", items, "--cards", "builtin", "--no-write"])
    assert gate.run_gate(args)[0] == 1  # default threshold 80 percent
    monkeypatch.setenv("EVAL_GATE_MIN_PLANTED_RECALL", "0.5")
    get_settings.cache_clear()
    assert gate.run_gate(args)[0] == 0


def test_the_gate_process_exits_non_zero_on_failure(tmp_path):
    good = [sys.executable, "-m", "eval.gate", "--no-write"]
    bad_items = write_items(tmp_path, item("missed", "planted_error", 9, "planted_error"))
    bad = [*good, "--items", bad_items, "--cards", "builtin"]
    env = {"PYTHONPATH": "src:.", "PATH": "/usr/bin:/bin"}
    ok = subprocess.run(good, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Eval gate passed." in ok.stdout
    fail = subprocess.run(bad, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert fail.returncode == 1, fail.stdout + fail.stderr
    assert "Eval gate FAILED." in fail.stdout and "below the threshold" in fail.stdout
