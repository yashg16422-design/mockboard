"""The deterministic numeric check (the second vote, ruling 1) and Hinglish number parsing."""

from __future__ import annotations

import pytest

from mockboard.brain.numbers import (
    NumericResult,
    Tolerance,
    canonical_unit,
    numeric_check,
    parse_number_phrase,
)

TOL = Tolerance(rel=0.05, abs_pp=1.0)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("30 percent", 30),
        ("2.5 crore", 2.5e7),
        ("dedh lakh", 1.5e5),
        ("saade teen crore", 3.5e7),
        ("1,23,456", 123456),
        ("2 crore 50 lakh", 2.5e7),
        ("5 hazaar crore", 5e10),
        ("teen sau", 300),
        ("bees lakh log", 2e6),
        ("koi number nahi", None),
    ],
)
def test_parse_number_phrase(text, expected):
    assert parse_number_phrase(text) == expected


@pytest.mark.parametrize(
    "unit, expected",
    [
        ("%", (1, "percent")),
        ("percentage points", (1, "percentage_points")),
        ("crore inr", (1e7, "inr")),
        ("lakh visitors", (1e5, "visitor")),
        (None, (1, "count")),
        ("districts", (1, "district")),
    ],
)
def test_canonical_unit(unit, expected):
    assert canonical_unit(unit) == expected


def check(cv, cu, cy, kv, ku, ky, **kw):
    return numeric_check(cv, cu, cy, kv, ku, ky, TOL, **kw).result


def test_percent_within_points_tolerance_matches():
    assert check(12.5, "percent", 2023, 12, "percent", 2023) == NumericResult.match


def test_percent_outside_tolerance_mismatches():
    assert check(30, "percent", 2023, 12, "percent", 2023) == NumericResult.mismatch


def test_percent_vs_percentage_points_is_incomparable():
    assert check(3, "percentage points", None, 3, "percent", None) == NumericResult.incomparable


def test_lakh_and_absolute_scale_agree():
    assert check(4_200_000, "visitors", 2022, 42, "lakh visitors", 2022) == NumericResult.match


def test_crore_mismatch_beyond_relative_tolerance():
    assert check(1200, "crore inr", 2025, 900, "crore inr", 2025) == NumericResult.mismatch


def test_relative_tolerance_edge():
    assert check(945, "crore inr", None, 900, "crore inr", None) == NumericResult.match  # 5.0%
    assert check(946, "crore inr", None, 900, "crore inr", None) == NumericResult.mismatch


def test_different_year_is_incomparable_not_mismatch():
    assert check(30, "percent", 2010, 12, "percent", 2023) == NumericResult.incomparable


def test_different_units_are_incomparable():
    assert check(42, "districts", None, 42, "lakh visitors", None) == NumericResult.incomparable


def test_missing_value_is_incomparable():
    assert check(None, "percent", None, 12, "percent", None) == NumericResult.incomparable


def test_date_claims():
    assert check(2019, "year", None, 2019, "year", None, is_date=True) == NumericResult.match
    assert check(2015, "year", None, 2019, "year", None, is_date=True) == NumericResult.mismatch


def test_nan_is_incomparable():
    assert check(float("nan"), "percent", None, 1, "percent", None) == NumericResult.incomparable
