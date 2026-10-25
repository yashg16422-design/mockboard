"""Eval metrics. Counts first (the merge gate uses counts), rates second.

* Planted-error recall: planted errors whose claim ended `contradicted` against the planted
  card id, over all planted errors.
* False corrections: claims labelled "true" that ended `contradicted` (the board would have
  challenged a correct statement), with an EXACT Clopper-Pearson upper bound on the rate.
* Move-class accuracy: predicted coarse move class == expected, over items.
* Claim F1: extracted claims matched one-to-one to expected claims.

No scipy needed: the binomial CDF is summed in log space and the bound found by bisection.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from eval.schema import ExpectedClaim, MoveClass
from mockboard.brain.numbers import Tolerance, canonical_unit
from mockboard.brain.schemas import Claim, ClaimKind, Move

MOVE_TO_CLASS: dict[Move, MoveClass] = {
    Move.fact_challenge: MoveClass.challenge,
    Move.counter: MoveClass.push_back,
    Move.steelman: MoveClass.push_back,
    Move.probe: MoveClass.follow_up,
    Move.pressure: MoveClass.follow_up,
    Move.move_on: MoveClass.follow_up,
    Move.reassure: MoveClass.follow_up,
    Move.clarify: MoveClass.follow_up,
    Move.interrupt: MoveClass.follow_up,
}


# ---------------------------------------------------------------------------
# Exact binomial bound
# ---------------------------------------------------------------------------


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), summed in log space for stability."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    terms = [
        math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
        for i in range(k + 1)
    ]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def clopper_pearson_upper(
    k: int, n: int, confidence: float = 0.95, *, two_sided: bool = True
) -> float:
    """Exact Clopper-Pearson upper limit for a binomial proportion with k successes in n.

    `two_sided=True` (default) gives the upper end of the conventional two-sided interval
    (tail alpha/2); `two_sided=False` gives the one-sided upper bound (tail alpha). The limit
    p solves P(X <= k | n, p) = tail; for k = n it is 1.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= k <= n:
        raise ValueError("k must be between 0 and n")
    if k == n:
        return 1.0
    tail = (1 - confidence) / (2 if two_sided else 1)
    if k == 0:
        return 1 - tail ** (1 / n)  # closed form
    lo, hi = k / n, 1.0
    for _ in range(200):  # the CDF falls monotonically in p
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > tail:
            lo = mid
        else:
            hi = mid
    return hi


# ---------------------------------------------------------------------------
# Claim matching
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


DEFAULT_TOLERANCE = Tolerance()


def claims_match(
    pred: Claim, exp: ExpectedClaim, tol: Tolerance = DEFAULT_TOLERANCE
) -> bool:
    """Numeric/date: same kind and value within tolerance (after scale words). Qualitative:
    at least half of the expected claim's content words appear in the predicted one."""
    if exp.kind in (ClaimKind.numeric, ClaimKind.date):
        if pred.kind != exp.kind or pred.value is None or exp.value is None:
            return False
        if exp.kind == ClaimKind.date:
            return int(pred.value) == int(exp.value)
        ps, pu = canonical_unit(pred.unit)
        es, eu = canonical_unit(exp.unit)
        a, b = pred.value * ps, exp.value * es
        if pu in ("percent", "percentage_points") or eu in ("percent", "percentage_points"):
            return pu == eu and abs(a - b) <= tol.abs_pp
        return abs(a - b) <= tol.rel * max(abs(b), 1e-12)
    want = _tokens(exp.claim_en)
    return bool(want) and len(want & _tokens(pred.claim_en)) * 2 >= len(want)


def match_claims(pred: Sequence[Claim], exp: Sequence[ExpectedClaim]) -> list[tuple[int, int]]:
    """Greedy one-to-one matching in expected order. Returns (pred index, exp index) pairs."""
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for j, e in enumerate(exp):
        for i, p in enumerate(pred):
            if i not in used and claims_match(p, e):
                used.add(i)
                pairs.append((i, j))
                break
    return pairs


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class Counts:
    items: int = 0
    planted_total: int = 0
    planted_caught: int = 0
    true_claims_total: int = 0  # true claims the listener extracted (denominator)
    true_claims_missed: int = 0  # labelled true but not extracted (reported separately)
    false_corrections: int = 0
    move_correct: int = 0
    claims_pred: int = 0
    claims_expected: int = 0
    claims_matched: int = 0
    balanced_items: int = 0
    balanced_pushback: int = 0
    fallbacks: int = 0

    def add(self, other: Counts) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


def ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def summarize(c: Counts, confidence: float = 0.95) -> dict[str, object]:
    precision = ratio(c.claims_matched, c.claims_pred)
    recall = ratio(c.claims_matched, c.claims_expected)
    f1 = None if not precision or not recall else 2 * precision * recall / (precision + recall)
    if precision == 0 or recall == 0:
        f1 = 0.0
    fc_upper = (
        clopper_pearson_upper(c.false_corrections, c.true_claims_total, confidence)
        if c.true_claims_total
        else None
    )
    return {
        "counts": c.__dict__.copy(),
        "planted_error_recall": ratio(c.planted_caught, c.planted_total),
        "false_corrections": c.false_corrections,
        "false_correction_rate": ratio(c.false_corrections, c.true_claims_total),
        "false_correction_cp_upper": fc_upper,
        "false_correction_cp_note": (
            f"exact Clopper-Pearson, two-sided {confidence:.0%} interval upper limit, "
            f"n={c.true_claims_total} extracted true claims"
        ),
        "move_class_accuracy": ratio(c.move_correct, c.items),
        "balanced_pushback_rate": ratio(c.balanced_pushback, c.balanced_items),
        "claim_precision": precision,
        "claim_recall": recall,
        "claim_f1": f1,
    }
