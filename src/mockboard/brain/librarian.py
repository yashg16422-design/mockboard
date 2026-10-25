"""Librarian: checks one claim against the fact cards held in the cached system prompt.

Two separate steps, on purpose:

1. `ask_librarian` - the model's vote (supported / contradicted / unverifiable + card_id).
   It depends only on the claim's content and the card set, so it is safe to cache by
   `claim_cache_key(claim, card_set_version)`.
2. `gate_verdict` - pure code that turns the vote into the final verdict. Per orchestrator
   ruling 1 (ADR-003), "contradicted" stands only when the model says contradicted AND the
   deterministic numeric check says mismatch. A model-only contradiction on a non-numeric
   claim becomes `possible_conflict`: shown in the report, never challenged live.
   No card means unverifiable, always.
"""

from __future__ import annotations

import hashlib
import json
import re

from mockboard.brain.cards import CardSet
from mockboard.brain.llm import LLMClient
from mockboard.brain.numbers import (
    NumericResult,
    Tolerance,
    canonical_unit,
    numeric_check,
)
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import Claim, ClaimKind, LibrarianVote, ModelVerdict, Verdict
from mockboard.brain.tools import LIBRARIAN_TOOL, LibrarianOutput


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%.]", " ", text.lower())).strip()


def claim_cache_key(claim: Claim, card_set_version: str) -> str:
    """Hash of the normalised claim content plus the card-set version.

    Turn ids, segment ids and the raw Hinglish wording are left out, so the same claim said
    twice (or re-run in an eval) hits the cache, and editing any card invalidates it.
    """
    scale, base = canonical_unit(claim.unit)
    payload = {
        "claim_en": _norm_text(claim.claim_en),
        "kind": claim.kind.value,
        "value": None if claim.value is None else round(claim.value * scale, 6),
        "unit": base if claim.value is not None else None,
        "year": claim.year,
        "cards": card_set_version,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return f"lib:{digest}"


def build_librarian_user(claim: Claim) -> str:
    fields = {
        "claim_en": claim.claim_en,
        "kind": claim.kind.value,
        "value": claim.value,
        "unit": claim.unit,
        "year": claim.year,
    }
    return (
        "Check this claim against the fact cards and call the tool.\n"
        + tag("claim", json.dumps(fields, ensure_ascii=False))
        + "\n"
        + tag("candidate_speech", claim.text)
    )


async def ask_librarian(
    llm: LLMClient, kit: PromptKit, claim: Claim, *, claim_key: str, trace_tag: str = ""
) -> LibrarianVote:
    """One librarian call. Raises `LLMError` subclasses; the graph node decides the fallback."""
    out = await llm.structured(
        name="librarian",
        system=kit.system("librarian"),
        user=build_librarian_user(claim),
        tool=LIBRARIAN_TOOL,
        output_model=LibrarianOutput,
        tag=trace_tag,
    )
    return LibrarianVote(
        claim_key=claim_key,
        model_verdict=out.verdict,
        card_id=out.card_id,
        rationale=out.rationale,
    )


def fallback_vote(claim_key: str, note: str) -> LibrarianVote:
    return LibrarianVote(
        claim_key=claim_key,
        model_verdict=ModelVerdict.unverifiable,
        is_fallback=True,
        note=note,
    )


def gate_verdict(
    claim: Claim, vote: LibrarianVote | None, cards: CardSet, tolerance: Tolerance
) -> Claim:
    """Deterministically combine the model vote with the card set and the numeric check.

    Truth table (first matching row wins):

    | condition                                            | final verdict      |
    |------------------------------------------------------|--------------------|
    | no vote, or the vote is a fallback                   | unverifiable       |
    | vote cites no card, or a card id that does not exist | unverifiable       |
    | model: unverifiable                                  | unverifiable       |
    | model: supported, numeric check = mismatch           | unverifiable       |
    | model: supported                                     | supported          |
    | model: contradicted, numeric/date claim + card value:|                    |
    |     numeric check = mismatch                         | contradicted       |
    |     numeric check = match or incomparable            | unverifiable       |
    | model: contradicted, anything else (non-numeric)     | possible_conflict  |
    """
    update: dict = {"model_verdict": None, "numeric_check": None, "card_id": None}
    if vote is None or vote.is_fallback:
        return claim.model_copy(
            update={**update, "verdict": Verdict.unverifiable,
                    "verdict_note": f"librarian unavailable {vote.note if vote else ''}".strip()}
        )
    update["model_verdict"] = vote.model_verdict
    card = cards.get(vote.card_id)
    if card is None:
        note = (
            "no card addresses this claim" if not vote.card_id else f"unknown card {vote.card_id!r}"
        )
        return claim.model_copy(
            update={**update, "verdict": Verdict.unverifiable, "verdict_note": note}
        )
    update["card_id"] = card.id

    check = None
    if claim.is_numeric_or_date and card.value is not None:
        check = numeric_check(
            claim.value, claim.unit, claim.year,
            card.value, card.unit, card.year,
            tolerance, is_date=claim.kind == ClaimKind.date,
        )
        update["numeric_check"] = check.result.value

    mv = vote.model_verdict
    if mv == ModelVerdict.unverifiable:
        verdict, note = Verdict.unverifiable, vote.rationale or "model: unverifiable"
    elif mv == ModelVerdict.supported:
        if check is not None and check.result == NumericResult.mismatch:
            verdict, note = Verdict.unverifiable, f"model and numeric check disagree: {check.note}"
        else:
            verdict, note = Verdict.supported, vote.rationale
    elif check is not None:
        if check.result == NumericResult.mismatch:
            verdict, note = Verdict.contradicted, f"model and numeric check agree: {check.note}"
        else:
            verdict, note = Verdict.unverifiable, f"numeric check did not confirm: {check.note}"
    else:
        verdict, note = Verdict.possible_conflict, (
            "model-only contradiction on a non-numeric claim; report only, never challenged"
        )
    return claim.model_copy(update={**update, "verdict": verdict, "verdict_note": note})
