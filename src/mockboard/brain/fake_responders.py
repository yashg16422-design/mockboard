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
from mockboard.brain.listener import honest_unknown_marker
from mockboard.brain.llm import FakeLLMClient
from mockboard.brain.numbers import (
    NumericResult,
    Tolerance,
    canonical_unit,
    nfc,
    numeric_check,
    to_ascii_digits,
)
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
    r"|million|billion|प्रतिशत|लाख|करोड़|करोड|हज़ार|हजार)?",
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
    "लेकिन",
    "परंतु",
    "मगर",
    "दूसरी तरफ",
    "दूसरी ओर",
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
# Garbled: only punctuation and single characters. A Devanagari word with vowel signs is a
# word, not a run of single letters, so the script's block counts as word characters.
_GARBLED = re.compile(r"^(?:[^\w\s\u0900-\u097f]|(?<![\w\u0900-\u097f])[\w\u0900-\u097f]"
                      r"(?![\w\u0900-\u097f])|\s)+$")


def _tag(text: str, name: str) -> str:
    m = re.search(_TAG.format(name), text, re.DOTALL)
    return m.group(1) if m else ""


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP and len(w) > 2}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.?!।])\s+", text) if s.strip()]


def _noun_unit(sentence: str) -> str | None:
    low = sentence.lower()
    if any(w in low for w in ("tourist", "arrival", "visitor", "paryatak", "पर्यटक")):
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
            if word in ("%", "percent", "per cent", "pratishat", "प्रतिशत"):
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
    speech = to_ascii_digits(nfc(_tag(user, "candidate_speech")))
    low = speech.lower()
    garbled = bool(speech.strip()) and bool(_GARBLED.match(speech))
    reasons = [s for s in _sentences(speech) if len(s.split()) >= 4][:4]
    has_both = any(m in low for m in _BALANCE_MARKERS)
    balance = "unclear" if len(speech.split()) < 8 else ("balanced" if has_both else "one_sided")
    out = {
        "stance": reasons[0][:120] if reasons else "",
        "reasons": [] if garbled else reasons,
        "claims": [] if garbled else extract_claims(speech),
        "balance": balance,
        "honest_unknown": any(m in low for m in _UNKNOWN_MARKERS) or honest_unknown_marker(speech),
        "concedes_error": bool(_CONCEDE.search(speech)),
        "garbled": garbled,
    }
    context = _tag(user, "debate_context")
    if context:  # debate mode (listener_debate.v1): add the optional debate fields
        out.update(_debate_fields(speech, json.loads(context), garbled))
    return out


# Debate listener stand-in (CTO_NOTES_04 §2). Word lists, not understanding.
_WARRANT = re.compile(
    r"\b(?:kyunki|kyonki|because|since|isliye|so that|which means|jisse|taaki|therefore|"
    r"as a result|for example|jaise ki|matlab)\b",
    re.IGNORECASE,
)
_REBUT_MARKERS = (
    "lekin", "but ", "however", "galat", "not true", "nahi", "disagree", "jawab", "par ",
    "doesn't", "isn't", "won't", "magar", "sahi nahi",
)
_CONCEDE_POINT = re.compile(
    r"\b(?:fair point|good point|i agree|i accept|maanta hoon|maanti hoon|maan leta|maan leti|"
    r"sahi baat|point taken|you(?:'re| are) right|aapki baat sahi|accha point|achha point)\b",
    re.IGNORECASE,
)


def _off_limits(speech: str) -> bool:
    from mockboard.modes.debate.screen import RULES

    if any(RULES[k].search(speech) for k in ("partisan", "caste", "communal")):
        return True
    groups = re.compile(
        r"\b(?:hindus?|muslims?|christians?|sikhs?|biharis?|bengalis?|tamils?|punjabis?|"
        r"north indians?|south indians?|women|men)\b[^.?!]*\b(?:are all|always|never|lazy|"
        r"inferior|worse|cannot)\b",
        re.IGNORECASE,
    )
    return bool(groups.search(speech))


def _debate_fields(speech: str, ctx: dict[str, Any], garbled: bool) -> dict[str, Any]:
    low = f" {speech.lower()} "
    sentences = _sentences(speech)
    arguments = []
    for sentence in sentences:
        if sentence.endswith("?") or len(sentence.split()) < 5:
            continue
        m = _WARRANT.search(sentence)
        if m:
            claim, warrant = sentence[: m.start()].strip(" ,"), sentence[m.start():].strip()
            arguments.append({"claim": claim or sentence, "warrant": warrant})
        else:
            arguments.append({"claim": sentence, "warrant": ""})
    rebutting = any(m in low for m in _REBUT_MARKERS)
    rebuts = []
    for arg in ctx.get("opponent_arguments") or []:
        hits = sum(1 for k in arg.get("keywords") or [] if k.lower() in low)
        if hits >= 2 or (hits >= 1 and rebutting):
            rebuts.append(arg["id"])
    question = next((s for s in reversed(sentences) if s.endswith("?")), "")
    words = len(speech.split())
    warranted = any(a["warrant"] for a in arguments)
    if rebuts and warranted and words >= 25:
        strength = "strong"
    elif words < 12 or not arguments:
        strength = "weak"
    else:
        strength = "adequate"
    if garbled:
        return {"arguments": [], "rebuts": [], "question_to_opponent": "",
                "concedes_point": False, "point_strength": "weak", "off_limits": False}
    return {
        "arguments": arguments[:4],
        "rebuts": rebuts,
        "question_to_opponent": question,
        "concedes_point": bool(_CONCEDE_POINT.search(speech)),
        "point_strength": strength,
        "off_limits": _off_limits(speech),
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


def _debate_actor_line(board: dict[str, Any], user: str) -> str:
    """Debate stand-in (actor_debate.v1): fixed shapes filled from the case file."""
    move = board.get("move", "rebut")
    args = board.get("use_arguments") or []
    sentences = _sentences(_tag(user, "candidate_speech"))
    point = next((x for x in sentences if _WARRANT.search(x)), max(sentences, key=len, default=""))
    speech = " ".join(point.split()[:8]).rstrip(".,?!")
    speech = speech[:1].lower() + speech[1:]
    first = args[0] if args else {"claim": "Is motion ke doosre pehlu bhi hain.", "reason": ""}
    claim = first["claim"][:1].lower() + first["claim"][1:]
    if move == Move.fact_challenge.value:
        f = json.loads(_tag(user, "fact_challenge") or "{}")
        return challenge_sentence(
            claim_figure=f.get("claim_figure", ""), card_figure=f.get("card_figure", ""),
            card_year=f.get("card_year"), source_short=f.get("source_short", ""),
            is_date=bool(f.get("is_date")), mix=f.get("language_mix"),
        )
    if move == Move.opening_statement.value:
        second = f" Doosri baat, {args[1]['claim'][:1].lower()}{args[1]['claim'][1:]}" if len(
            args) > 1 else ""
        return (f"Aap kehte hain ki {speech}, aur yeh baat samajh aati hai. Lekin {claim} "
                f"{first.get('reason', '')}{second} Ab aap jawab dijiye.")
    if move == Move.rebut.value:
        answer = "Aapke sawaal ka seedha jawab: haan, yeh fark padta hai. " if board.get(
            "answer_first") else ""
        return (f"{answer}Aapka kehna hai ki {speech}. Lekin {claim} "
                f"{first.get('reason', '')} Iska jawab kya hai?")
    if move == Move.concede_point.value:
        return f"Yeh point aapka mazboot hai, ise maanna padega. Lekin {claim}"
    if move == Move.cross_question.value:
        if board.get("cross_on") == "warrant":
            return f"Aap kehte hain {speech}. Yeh kyun sach hai? Aapka tark kya hai?"
        return f"Aapne mere is point ka jawab nahi diya: {first['claim']} Iska jawab kya hai?"
    if move == Move.closing_statement.value:
        concede = board.get("concede_first") or "aapne kuch baatein achhi rakhi."
        return f"Maanna padega ki {concede} Phir bhi, {claim} Isliye mera paksh wahi hai."
    if move == Move.steelman.value:
        return "Ek sentence mein mere paksh ka sabse mazboot tark bataiye."
    if move == Move.redirect.value:
        return ("Chaliye, parties aur vyaktiyon ko alag rakhte hain. Motion par wapas aaiye: "
                f"{board.get('motion', '')}")
    if move == Move.move_on.value:
        if board.get("next_round") == "closing":
            return ("Samay ho gaya. Ab closing statements: pehle aap, apni sabse mazboot baat "
                    "rakhiye.")
        return "Shukriya. Debate yahin samaapt hoti hai."
    if move == Move.reassure.value:
        return "Koi baat nahi, aaram se. Apna sabse mazboot point rakhiye."
    if move == Move.clarify.value:
        return "Maaf kijiye, main theek se sun nahi paaya. Kya aap phir se bata sakte hain?"
    return "Ek minute, main beech mein rokta hoon. Apne mukhya point par aaiye."


def actor_responder(system: str, user: str) -> str:
    debate = _tag(user, "debate_board")
    if debate:
        return _debate_actor_line(json.loads(debate), user)
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


# Debate judge stand-in (judge_debate.v1). Word rules over the session block, clearly labelled
# as such in every rationale; it says nothing about how Nova scores a debate.
_DEBATE_RATIONALE: dict[str, dict[str, str]] = {
    "en": {
        "clarity": "Stand-in (rule-based, not a model): the user stated a position with reasons.",
        "evidence_contradicted": "Stand-in (rule-based, not a model): a figure was contradicted "
                                 "by a fact card.",
        "evidence_supported": "Stand-in (rule-based, not a model): figures matched the fact "
                              "cards.",
        "evidence_honest": "Stand-in (rule-based, not a model): the user said honestly what "
                           "they did not know.",
        "evidence_reason": "Stand-in (rule-based, not a model): claims came with reasons.",
        "rebuttal_answered": "Stand-in (rule-based, not a model): the user answered the "
                             "opponent's actual arguments.",
        "rebuttal_missed": "Stand-in (rule-based, not a model): no opponent argument was "
                           "answered directly.",
        "responsive": "Stand-in (rule-based, not a model): the user replied to the board's "
                      "question.",
        "fair": "Stand-in (rule-based, not a model): the user granted the opponent a fair point.",
        "composure": "Stand-in (rule-based, not a model): steady to the last turn.",
        "insufficient": "Stand-in (rule-based, not a model): not shown in this debate.",
        "best": "Stand-in (rule-based, not a model): the first sentence in a rebuttal that "
                "gives a reason.",
    },
    "hi": {
        "clarity": "ऑफ़लाइन नियम-आधारित अनुमान: उपयोगकर्ता ने कारणों के साथ अपना पक्ष रखा।",
        "evidence_contradicted": "ऑफ़लाइन नियम-आधारित अनुमान: एक आँकड़ा फ़ैक्ट कार्ड से मेल "
                                 "नहीं खाया।",
        "evidence_supported": "ऑफ़लाइन नियम-आधारित अनुमान: आँकड़े फ़ैक्ट कार्ड से मेल खाए।",
        "evidence_honest": "ऑफ़लाइन नियम-आधारित अनुमान: उपयोगकर्ता ने ईमानदारी से बताया कि "
                           "उन्हें क्या नहीं पता।",
        "evidence_reason": "ऑफ़लाइन नियम-आधारित अनुमान: दावों के साथ कारण दिए गए।",
        "rebuttal_answered": "ऑफ़लाइन नियम-आधारित अनुमान: उपयोगकर्ता ने विपक्ष के असली तर्कों "
                             "का जवाब दिया।",
        "rebuttal_missed": "ऑफ़लाइन नियम-आधारित अनुमान: विपक्ष के किसी तर्क का सीधा जवाब "
                           "नहीं दिया गया।",
        "responsive": "ऑफ़लाइन नियम-आधारित अनुमान: उपयोगकर्ता ने बोर्ड के प्रश्न का उत्तर दिया।",
        "fair": "ऑफ़लाइन नियम-आधारित अनुमान: उपयोगकर्ता ने विपक्ष की एक सही बात मानी।",
        "composure": "ऑफ़लाइन नियम-आधारित अनुमान: अंत तक संयम बना रहा।",
        "insufficient": "ऑफ़लाइन नियम-आधारित अनुमान: इस बहस में यह नहीं दिखा।",
        "best": "ऑफ़लाइन नियम-आधारित चुनाव: खंडन का पहला वाक्य जो कारण भी देता है।",
    },
}
_FAKE_WARRANT = re.compile(r"\b(?:kyunki|kyonki|because|since|isliye|therefore|taaki)\b|क्योंकि",
                           re.IGNORECASE)


def debate_judge_responder(system: str, user: str) -> dict[str, Any]:
    language = _tag(user, "report_language").strip() or "en"
    texts = _DEBATE_RATIONALE.get(language, _DEBATE_RATIONALE["en"])
    rows = json.loads(_tag(user, "debate_session") or "[]")
    skipped = {"redirect", "clarify"}
    spoke = [
        r for r in rows
        if r.get("user_speech", "").strip()
        and ((r.get("board_reply") or {}).get("move") not in skipped)
    ]

    def quote(row: dict[str, Any]) -> str:
        return " ".join(row["user_speech"].split()[:6])

    def pick(rule: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        return next((r for r in spoke if rule(r)), None)

    verdicts = [c["verdict"] for r in spoke for c in r.get("claims", [])]
    plan: list[tuple[str, str, dict[str, Any] | None, str]] = []  # dim, score, row, text key
    plan.append(("clarity_structure", "3", spoke[0] if spoke else None, "clarity"))
    if "contradicted" in verdicts:
        row = pick(lambda r: any(c["verdict"] == "contradicted" for c in r.get("claims", [])))
        plan.append(("evidence_use", "2", row, "evidence_contradicted"))
    elif "supported" in verdicts:
        row = pick(lambda r: any(c["verdict"] == "supported" for c in r.get("claims", [])))
        plan.append(("evidence_use", "4", row, "evidence_supported"))
    elif (row := pick(lambda r: r.get("honest_unknown"))) is not None:
        plan.append(("evidence_use", "3", row, "evidence_honest"))
    else:
        row = pick(lambda r: bool(_FAKE_WARRANT.search(r["user_speech"])))
        plan.append(("evidence_use", "3", row, "evidence_reason"))
    row = pick(lambda r: bool(r.get("opponent_arguments_answered")))
    if row is not None:
        plan.append(("rebuttal_quality", "4", row, "rebuttal_answered"))
    else:
        plan.append(("rebuttal_quality", "2", spoke[1] if len(spoke) > 1 else None,
                     "rebuttal_missed"))
    row = pick(lambda r: str(r.get("board_said_before", "")).rstrip().endswith("?"))
    plan.append(("responsiveness", "3", row, "responsive"))
    plan.append(("fairness_to_other_side", "4", pick(lambda r: r.get("user_conceded_a_point")),
                 "fair"))
    plan.append(("composure", "3", spoke[-1] if spoke else None, "composure"))

    scores, evidence = [], []
    for dim, score, row, key in plan:
        if row is None:
            scores.append({"dimension": dim, "score": "insufficient_evidence",
                           "rationale": texts["insufficient"]})
            continue
        scores.append({"dimension": dim, "score": score, "rationale": texts[key]})
        evidence.append({"dimension": dim, "turn_id": row["turn_id"], "quote": quote(row)})

    best_turn, best_quote = "", ""
    for row in sorted(spoke, key=lambda r: not r.get("opponent_arguments_answered")):
        hit = next((s for s in _sentences(row["user_speech"])
                    if len(s.split()) >= 6 and _FAKE_WARRANT.search(s)), None)
        if hit:
            best_turn, best_quote = row["turn_id"], hit
            break
    return {
        "scores": scores,
        "evidence": evidence,
        "best_line_turn_id": best_turn,
        "best_line_quote": best_quote,
        "best_line_reason": texts["best"] if best_quote else "",
    }


# ---------------------------------------------------------------------------
# Debate: motion screen classifier and custom case file (stand-ins for Nova)
# ---------------------------------------------------------------------------

# The fake classifier stands in for Nova's MEANING-level judgement with a second, wider word
# list. It says nothing about how well Nova screens; eval/motions marks every decision made at
# this layer as "needs real Nova".
_FAKE_SCREEN: dict[str, re.Pattern[str]] = {
    "partisan": re.compile(
        r"\b(?:ministers?|mlas?|mps?|politicians?|political|opposition|ruling|leaders?|"
        r"left-wing|right-wing|liberals|nationalists?|anti-national|bhakts?|governance|"
        r"party|parties|cabinet|parliament)\b",
        re.IGNORECASE,
    ),
    "group_targeting": re.compile(
        r"\b(?:biharis?|bengalis?|tamils?|punjabis?|gujaratis?|marathis?|kashmiris?|"
        r"north indians?|south indians?|northeasterners?|migrants?|outsiders|tribals?|"
        r"women|men|girls|boys)\b.*\b(?:are|taking|take|cannot|can't|should not|shouldn't|"
        r"worse|better|inferior|lazy)\b",
        re.IGNORECASE,
    ),
    "violence": re.compile(r"\b(?:war|wars|army|military|fight\w*)\b", re.IGNORECASE),
    "private_individual": re.compile(r"\b(?:my|our) (?:neighbou?r|teacher|boss|friend|classmate)"
                                     r"|\b[A-Z][a-z]+ [A-Z][a-z]+\b"),
}
_LOADED = re.compile(
    r"^(?:this house believes\s+)?(?P<subject>.+?)\s+(?P<verb>is|are)\s+(?:a\s+)?"
    r"(?:total\s+|complete\s+)?(?:disaster|useless|stupid|pathetic|rubbish|a joke|"
    r"waste of time)\b.*$",
    re.IGNORECASE,
)


def motion_screen_responder(system: str, user: str) -> dict[str, Any]:
    motion = _tag(user, "motion").strip()
    body = re.sub(r"^This House\s+", "", motion)
    for category, pattern in _FAKE_SCREEN.items():
        if pattern.search(body):
            return {"decision": "refuse", "category": category,
                    "reason": f"stand-in classifier: {category}", "neutral_rewording": ""}
    m = _LOADED.match(motion)
    if m:
        do = "do" if m.group("verb").lower() == "are" else "does"
        return {"decision": "reframe", "category": "loaded",
                "reason": "stand-in classifier: loaded wording",
                "neutral_rewording": f"This House believes {m.group('subject')} {do} more harm "
                "than good."}
    return {"decision": "allow", "category": "none", "reason": "stand-in classifier: allowed",
            "neutral_rewording": ""}


def case_file_responder(system: str, user: str) -> dict[str, Any]:
    side = _tag(user, "side").strip()
    verb = "helps" if side == "for" else "costs more than it helps"
    return {
        "arguments": [
            {"claim": f"The proposal {verb} the people it affects most.",
             "warrant": "Its effects fall first on everyday life, not on abstractions.",
             "keywords": ["people", "everyday", "affects"]},
            {"claim": "There is a simpler way to look at this question.",
             "warrant": "When the main trade-off is clear, the answer follows from it.",
             "keywords": ["simple", "trade-off", "clear"]},
            {"claim": "Experience elsewhere points the same way.",
             "warrant": "Similar choices tend to show the same pattern over time.",
             "keywords": ["experience", "pattern", "elsewhere"]},
        ]
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def fake_responders(
    cards: CardSet, tolerance: Tolerance | None = None
) -> dict[str, Callable[[str, str], Any]]:
    """Call name -> rule-based responder, for every call name the brain uses."""
    return {
        "listener": listener_responder,
        "librarian": make_librarian_responder(cards, tolerance),
        "actor": actor_responder,
        "opener": opener_responder,
        "judge": judge_responder,
        "judge_debate": debate_judge_responder,
        "motion_screen": motion_screen_responder,
        "case_file": case_file_responder,
    }


def make_fake_llm(
    cards: CardSet, tolerance: Tolerance | None = None, **kwargs: Any
) -> FakeLLMClient:
    """A FakeLLMClient with rule-based responders for every call name the brain uses."""
    return FakeLLMClient(responders=fake_responders(cards, tolerance), **kwargs)
