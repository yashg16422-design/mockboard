"""Rule-based stand-ins for Nova, used by `--fake` mode, the eval runner's default and tests.

These are NOT a model and say nothing about Nova's quality. They exist so the whole
pipeline (graph, gate, strategist, actor guards, judge assembly, traces) runs offline and
deterministically. Each responder reads only what the real model would see: the system
text and the user message, plus the card set the system prompt was built from.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from mockboard.brain.cards import CardSet
from mockboard.brain.llm import FakeLLMClient
from mockboard.brain.numbers import NumericResult, Tolerance, canonical_unit, numeric_check
from mockboard.brain.schemas import ClaimKind, Dimension, FactCard, Move
from mockboard.brain.spoken import challenge_sentence

_TAG = r"<{0}>\n(.*?)\n</{0}>"
_STOP = {
    "the",
    "a",
    "an",
    "of",
    "in",
    "is",
    "was",
    "are",
    "to",
    "and",
    "for",
    "by",
    "on",
    "its",
    "test",
    "card",
    "hai",
    "ka",
    "ki",
    "ke",
    "mein",
    "me",
    "se",
    "aur",
    "tha",
    "ne",
    "share",
    "percent",
    "year",
    "lakh",
    "crore",
}
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_NUM_UNIT = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(%|percent|per cent|pratishat|lakh|lac|crore|karod|hazaar|thousand"
    r"|million|billion)?",
    re.IGNORECASE,
)
_BALANCE_MARKERS = (
    "lekin",
    "but",
    "however",
    "on the other hand",
    "doosri taraf",
    "magar",
    "par saath hi",
    "at the same time",
    "both",
)
_UNKNOWN_MARKERS = (
    "pata nahi",
    "i don't know",
    "i do not know",
    "nahi pata",
    "not sure",
    "maloom nahi",
)
# "I was wrong" in English and Hinglish: a first-person subject near an error word.
_CONCEDE = re.compile(
    r"\b(?:mera|meri|maine|my|i was|i am|i'm)\b[^.?!]{0,30}\b(?:galat|wrong|mistake|galti)\b"
    r"|\bi stand corrected\b|\bmeri galti\b",
    re.IGNORECASE,
)
_GARBLED = re.compile(r"^(?:[^\w\s]|\b\w\b|\s)+$")


def _tag(text: str, name: str) -> str:
    m = re.search(_TAG.format(name), text, re.DOTALL)
    return m.group(1) if m else ""


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP and len(w) > 2}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.?!।])\s+", text) if s.strip()]


def _noun_unit(sentence: str) -> str | None:
    low = sentence.lower()
    if any(w in low for w in ("tourist", "arrival", "visitor", "paryatak")):
        return "visitors"
    if "district" in low or "zile" in low:
        return "districts"
    if any(w in low for w in ("rupee", "rupaye", "rs", "₹", "budget", "inr")):
        return "inr"
    return None


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


def extract_claims(text: str) -> list[dict[str, Any]]:
    """Find number and year statements, the way the listener prompt asks Nova to."""
    claims: list[dict[str, Any]] = []
    for sentence in _sentences(text):
        years = [int(y) for y in _YEAR.findall(sentence)]
        found_numeric = False
        for m in _NUM_UNIT.finditer(sentence):
            raw, word = m.group(1).replace(",", ""), (m.group(2) or "").lower()
            value = float(raw)
            if not word and int(value) in years and value.is_integer():
                continue  # a bare year; handled below
            if not word and value < 10:
                continue  # small bare numbers ("2 reasons") are not claims
            if word in ("%", "percent", "per cent", "pratishat"):
                unit = "percent"
            else:
                scale, _ = canonical_unit(word or None)
                value *= scale
                unit = _noun_unit(sentence)
            year = next((y for y in years if y != int(float(raw))), None)
            claims.append(
                {
                    "text": sentence,
                    "claim_en": sentence,
                    "kind": "numeric",
                    "value": value,
                    "unit": unit,
                    "year": year,
                }
            )
            found_numeric = True
        if not found_numeric and len(years) == 1:
            claims.append(
                {
                    "text": sentence,
                    "claim_en": sentence,
                    "kind": "date",
                    "value": float(years[0]),
                    "unit": "year",
                    "year": years[0],
                }
            )
    return claims


def listener_responder(system: str, user: str) -> dict[str, Any]:
    speech = _tag(user, "candidate_speech")
    low = speech.lower()
    garbled = bool(speech.strip()) and bool(_GARBLED.match(speech))
    reasons = [s for s in _sentences(speech) if len(s.split()) >= 4][:4]
    has_both = any(m in low for m in _BALANCE_MARKERS)
    balance = "unclear" if len(speech.split()) < 8 else ("balanced" if has_both else "one_sided")
    return {
        "stance": reasons[0][:120] if reasons else "",
        "reasons": [] if garbled else reasons,
        "claims": [] if garbled else extract_claims(speech),
        "balance": balance,
        "honest_unknown": any(m in low for m in _UNKNOWN_MARKERS),
        "concedes_error": bool(_CONCEDE.search(speech)),
        "garbled": garbled,
    }


# ---------------------------------------------------------------------------
# Librarian
# ---------------------------------------------------------------------------


def best_card(claim: dict[str, Any], cards: CardSet) -> FactCard | None:
    """The card about the same thing: same kind, same canonical unit, most shared words."""
    kind = claim.get("kind")
    if kind not in (ClaimKind.numeric.value, ClaimKind.date.value):
        return None
    _, claim_base = canonical_unit(claim.get("unit"))
    words = _words(claim.get("claim_en", ""))
    best, best_overlap = None, 1  # need at least 2 shared content words
    for card in cards.cards.values():
        if card.kind.value != kind or card.value is None:
            continue
        _, card_base = canonical_unit(card.unit)
        if kind == ClaimKind.numeric.value and card_base != claim_base:
            continue
        overlap = len(words & _words(card.statement + " " + " ".join(card.aliases)))
        if overlap > best_overlap:
            best, best_overlap = card, overlap
    return best


def make_librarian_responder(cards: CardSet, tolerance: Tolerance | None = None) -> Callable:
    tol = tolerance or Tolerance()

    def librarian_responder(system: str, user: str) -> dict[str, Any]:
        claim = json.loads(_tag(user, "claim") or "{}")
        card = best_card(claim, cards)
        if card is None:
            return {"card_id": "", "verdict": "unverifiable", "rationale": "no card addresses it"}
        check = numeric_check(
            claim.get("value"),
            claim.get("unit"),
            claim.get("year"),
            card.value,
            card.unit,
            card.year,
            tol,
            is_date=claim.get("kind") == ClaimKind.date.value,
        )
        verdict = {
            NumericResult.match: "supported",
            NumericResult.mismatch: "contradicted",
            NumericResult.incomparable: "unverifiable",
        }[check.result]
        return {"card_id": card.id, "verdict": verdict, "rationale": f"card says: {card.statement}"}

    return librarian_responder


# ---------------------------------------------------------------------------
# Actor and opener (streamed text)
# ---------------------------------------------------------------------------

_ACTOR_LINES: dict[str, str] = {
    Move.probe.value: "Achha. Aap jo keh rahe hain, uska zameeni asar kaise dikhega?",
    Move.counter.value: (
        "Lekin kai log kehte hain iska nuksaan bhi hai. Us paksh ko aap kaise dekhenge?"
    ),
    Move.steelman.value: (
        "Theek hai. Ab jis paksh se aap asahmat hain, uska sabse mazboot tark kya hoga?"
    ),
    Move.pressure.value: "Koi ek thos udaharan dijiye. Aapki baat ka aadhar kya hai?",
    Move.reassure.value: "Koi baat nahi, aaram se. Aap apne anubhav se kya kehna chahenge?",
    Move.clarify.value: "Maaf kijiye, main samajh nahi paaya. Kya aap phir se bata sakte hain?",
    Move.interrupt.value: "Ek minute, main beech mein rokta hoon. Aapka mukhya point kya hai?",
}


def actor_responder(system: str, user: str) -> str:
    ctx = json.loads(_tag(user, "board_context") or "{}")
    move = ctx.get("move", Move.probe.value)
    if move == Move.fact_challenge.value:
        # Same shape as the real template (actor.v2), from the structured fields only.
        f = json.loads(_tag(user, "fact_challenge") or "{}")
        return challenge_sentence(
            claim_figure=f.get("claim_figure", ""),
            card_figure=f.get("card_figure", ""),
            card_year=f.get("card_year"),
            source_short=f.get("source_short", ""),
            is_date=bool(f.get("is_date")),
            mix=f.get("language_mix"),
        )
    if move == Move.move_on.value:
        nxt = ctx.get("next_topic")
        if not nxt or nxt == "close":
            return "Shukriya, aapka interview yahin samaapt hota hai. Aapka din shubh ho?"
        return (
            f"Theek hai, shukriya. Ab {nxt.replace('_', ' ')} par aate hain: is kshetra mein "
            "aapko kaunsi chunauti sabse zaroori lagti hai, aur kyun?"
        )
    return _ACTOR_LINES.get(move, _ACTOR_LINES[Move.probe.value])


def opener_responder(system: str, user: str) -> str:
    form = dict(
        line.split(": ", 1) for line in _tag(user, "application_form").splitlines() if ": " in line
    )
    place = form.get("district") or form.get("home_state", "aapke rajya")
    subject = form.get("degree_subject", "aapke vishay")
    return (
        f"Namaste, baithiye. Aap {place} se hain aur {subject} padha hai. Aapke hisaab se "
        f"{subject} ki samajh {place} ki kis ek samasya ko suljhane mein kaise madad kar sakti hai?"
    )


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

_RATIONALE = {
    "en": "Rule-based offline stand-in score from the session transcript (not a model judgement).",
    "hi": "यह ऑफ़लाइन नियम-आधारित अनुमान है, किसी मॉडल का निर्णय नहीं।",
}


def judge_responder(system: str, user: str) -> dict[str, Any]:
    language = _tag(user, "report_language").strip() or "en"
    rows = json.loads(_tag(user, "session") or "[]")
    answered = [r for r in rows if r.get("candidate_transcript", "").strip()]
    verdicts = [c["verdict"] for r in rows for c in r.get("claims", [])]
    checked = [v for v in verdicts if v in ("supported", "contradicted")]
    scores, evidence = [], []
    for dim in Dimension:
        if dim == Dimension.factual_accuracy:
            score = (
                str(max(1, 5 - 2 * checked.count("contradicted")))
                if checked
                else "insufficient_evidence"
            )
        elif dim in (Dimension.social_cohesion_leadership,) or not answered:
            score = "insufficient_evidence"
        else:
            score = "3"
        scores.append(
            {
                "dimension": dim.value,
                "score": score,
                "rationale": _RATIONALE.get(language, _RATIONALE["en"]),
            }
        )
        if score != "insufficient_evidence":
            row = answered[0] if dim != Dimension.composure else answered[-1]
            quote = " ".join(row["candidate_transcript"].split()[:6])
            evidence.append({"dimension": dim.value, "turn_id": row["turn_id"], "quote": quote})
    return {
        "scores": scores,
        "evidence": evidence,
        "composure_moments": [],
        "model_answer": _RATIONALE.get(language, _RATIONALE["en"]),
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_fake_llm(
    cards: CardSet, tolerance: Tolerance | None = None, **kwargs: Any
) -> FakeLLMClient:
    """A FakeLLMClient with rule-based responders for every call name the brain uses."""
    return FakeLLMClient(
        responders={
            "listener": listener_responder,
            "librarian": make_librarian_responder(cards, tolerance),
            "actor": actor_responder,
            "opener": opener_responder,
            "judge": judge_responder,
        },
        **kwargs,
    )
