"""The request detector (CTO_NOTES_06 section 3) and acceptance A1b over the labelled corpus.

The corpus is synthetic. Its recall says nothing about real Prisma text.
Run `pytest -s tests/test_requests.py -k corpus` to print the table.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from mockboard.brain.requests import (
    _THIRD_PERSON,
    END_MAX_WORDS,
    SHORT_ANSWER_WORDS,
    TAIL_WORDS,
    RequestHit,
    RequestKind,
    detect_request,
)

CORPUS_PATH = Path(__file__).parent / "data" / "request_corpus.jsonl"
SAMPLE_FORM = Path(__file__).parent.parent / "examples" / "forms" / "sample_form.json"
KINDS = tuple(kind.value for kind in RequestKind)
NONE = "none"
# Each kind's recall on the rule-tuning split must not fall below this.
DEV_RECALL_FLOOR = 0.95
FILLER = "the district has good roads and many small farms near the river "


def _load_corpus() -> list[dict]:
    with CORPUS_PATH.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


CORPUS = _load_corpus()


def _got(text: str) -> str:
    hit = detect_request(text)
    return hit.kind.value if hit else NONE


def _table(rows: list[dict]) -> dict[str, tuple[int, int, int]]:
    """Per kind: lines labelled that kind, of those detected as it, false hits on other lines."""
    table: dict[str, tuple[int, int, int]] = {}
    for kind in KINDS:
        positives = [r for r in rows if r["expect"] == kind]
        recalled = sum(1 for r in positives if _got(r["text"]) == kind)
        false_hits = sum(1 for r in rows if r["expect"] != kind and _got(r["text"]) == kind)
        table[kind] = (len(positives), recalled, false_hits)
    return table


def _print_table(title: str, rows: list[dict]) -> None:
    print(f"\n{title} ({len(rows)} lines, synthetic; says nothing about real Prisma text)")
    print(f"{'kind':<18}{'lines':>6}{'hit':>5}{'recall':>8}{'false+':>7}")
    for kind, (lines, hit, false_hits) in _table(rows).items():
        recall = hit / lines if lines else 0.0
        print(f"{kind:<18}{lines:>6}{hit:>5}{recall:>8.2f}{false_hits:>7}")
    negatives = [r for r in rows if r["expect"] == NONE]
    fired = sum(1 for r in negatives if _got(r["text"]) != NONE)
    print(f"negatives: {len(negatives)} lines, {fired} fired")


# ---------------------------------------------------------------------------
# The corpus (A1b)
# ---------------------------------------------------------------------------


def test_corpus_shape():
    assert len(CORPUS) >= 150
    assert len({row["id"] for row in CORPUS}) == len(CORPUS)
    assert all(row["synthetic"] is True for row in CORPUS)
    assert {row["expect"] for row in CORPUS} == {*KINDS, NONE}
    assert {row["lang"] for row in CORPUS} == {"hinglish", "english", "devanagari", "garbled"}
    for kind in KINDS:
        langs = {row["lang"] for row in CORPUS if row["expect"] == kind}
        assert langs == {"hinglish", "english", "devanagari", "garbled"}, kind


@pytest.mark.parametrize(
    "text, kind",
    [
        ("tourism ke alawa kisi aur topic pe baat karte hain", "change_topic"),
        ("can we move on?", "change_topic"),
        ("kisi aur topic pe baat karte hain", "change_topic"),
        ("let's talk about something else", "change_topic"),
        ("next question please", "skip"),
        ("I'd rather not attempt this", "skip"),
        ("is sawal ko chhod dete hain", "skip"),
        ("please repeat", "repeat"),
        ("phir se boliye", "repeat"),
        ("फिर से बताइए", "repeat"),
        ("what do you mean", "clarify_question"),
        ("sawal samajh nahi aaya", "clarify_question"),
        ("matlab?", "clarify_question"),
        ("I want to end the interview", "end"),
        ("interview yahin khatam karte hain", "end"),
    ],
)
def test_brief_and_design_sentences_hit(text, kind):
    assert _got(text) == kind


def test_request_corpus():
    """Zero false `end`, zero false `change_topic` on negatives, recall reported."""
    _print_table("request corpus, dev split", [r for r in CORPUS if r["split"] == "dev"])
    _print_table("request corpus, holdout split", [r for r in CORPUS if r["split"] == "holdout"])
    _print_table("request corpus, all", CORPUS)

    false_end = [r["id"] for r in CORPUS if r["expect"] != "end" and _got(r["text"]) == "end"]
    assert false_end == []

    negatives = [r for r in CORPUS if r["expect"] == NONE]
    assert [r["id"] for r in negatives if _got(r["text"]) == "change_topic"] == []

    dev = _table([r for r in CORPUS if r["split"] == "dev"])
    for kind, (lines, hit, _false_hits) in dev.items():
        assert hit / lines >= DEV_RECALL_FLOOR, kind


def test_corpus_negatives_fire_no_kind():
    assert [r["id"] for r in CORPUS if r["expect"] == NONE and _got(r["text"]) != NONE] == []


def test_corpus_avoid_terms():
    for row in CORPUS:
        if "avoid" not in row:
            continue
        hit = detect_request(row["text"])
        if hit is None or hit.kind != RequestKind.CHANGE_TOPIC:
            continue  # a recall miss, counted in the table
        assert list(hit.avoid_terms) == row["avoid"], row["id"]


def test_corpus_covers_each_kind_and_many_negatives():
    counts = Counter(row["expect"] for row in CORPUS)
    assert all(counts[kind] >= 15 for kind in KINDS)
    assert counts[NONE] >= 60


# ---------------------------------------------------------------------------
# Rules from the design
# ---------------------------------------------------------------------------


def test_returns_a_frozen_hit_with_pattern_id():
    hit = detect_request("tourism ke alawa kisi aur topic pe baat karte hain")
    assert isinstance(hit, RequestHit)
    assert hit.kind == "change_topic"
    assert hit.avoid_terms == ("tourism",)
    assert hit.pattern_id
    with pytest.raises(AttributeError):
        hit.kind = RequestKind.END


@pytest.mark.parametrize("text", ["", "   ", "...", "।", "हम्म"])
def test_empty_or_no_words_is_no_request(text):
    assert detect_request(text) is None


def test_ke_alawa_alone_never_fires():
    assert detect_request("tourism ke alawa agriculture bhi important hai") is None
    assert detect_request("tourism ke alawa") is None


def test_position_rule_midway_phrase_in_long_answer_is_not_a_request():
    tail = FILLER * 2
    assert len(tail.split()) > TAIL_WORDS
    text = f"{FILLER}{FILLER}{FILLER}can we move on from this {tail}"
    assert detect_request(text) is None


def test_position_rule_phrase_in_last_twelve_words_counts():
    text = f"{FILLER * 4}so can we move on"
    assert len(text.split()) > SHORT_ANSWER_WORDS
    assert _got(text) == "change_topic"


def test_move_on_to_a_named_topic_counts():
    assert _got("can we move on to agriculture") == "change_topic"


def test_short_answer_counts_wherever_the_phrase_sits():
    assert _got("can we move on, I really have nothing useful to add here") == "change_topic"


def test_end_needs_twelve_words_or_fewer():
    short = "sir I am not well, I want to end the interview"
    assert len(short.split()) <= END_MAX_WORDS
    assert _got(short) == "end"
    long = "sir I am really not feeling well today so I want to end the interview"
    assert len(long.split()) > END_MAX_WORDS
    assert _got(long) != "end"


@pytest.mark.parametrize(
    "text",
    [
        "I will stop here",
        "main yahin khatam karta hoon",
        "that is all from my side",
        "let's stop here",
        "we need to end this conflict",
    ],
)
def test_ending_an_answer_is_not_ending_the_interview(text):
    assert _got(text) != "end"


@pytest.mark.parametrize(
    "text, terms",
    [
        ("tourism ke alawa kisi aur topic pe baat karte hain", ("tourism",)),
        ("rural tourism ke alawa kuch aur puchiye", ("rural tourism",)),
        ("tourism topic ke alawa kisi aur topic pe baat karte hain", ("tourism",)),
        ("other than tourism can we talk about something else", ("tourism",)),
        ("can we move on from this", ()),
        ("is topic ke alawa kisi aur topic pe baat karte hain", ()),
        ("can we change the topic", ()),
        ("पर्यटन के अलावा किसी और विषय पर बात करते हैं", ("पर्यटन",)),
    ],
)
def test_avoid_terms(text, terms):
    hit = detect_request(text)
    assert hit is not None
    assert hit.avoid_terms == terms


def test_only_change_topic_carries_avoid_terms():
    hit = detect_request("next question please")
    assert hit is not None
    assert hit.avoid_terms == ()


def test_avoid_term_keeps_the_spoken_spelling():
    hit = detect_request("Turism ke alava kisi aur topic pe baat karte hain")
    assert hit is not None
    assert hit.avoid_terms == ("turism",)


@pytest.mark.parametrize(
    "garbled, kind",
    [
        ("please repeet", "repeat"),  # one substitution
        ("kisi aur topicc pe baat karte hain", "change_topic"),  # one insertion
        ("intervie khatam karte hain", "end"),  # one deletion
    ],
)
def test_edit_distance_one_reaches_key_words(garbled, kind):
    assert _got(garbled) == kind


@pytest.mark.parametrize("word", ["repeal", "tropic", "toxic"])
def test_real_words_near_a_key_word_stay_themselves(word):
    assert detect_request(f"please {word}") is None
    assert detect_request(f"can we talk about {word}") is None


def test_short_tokens_get_no_edit_distance():
    assert detect_request("plz rpt") is None


def test_composed_and_decomposed_devanagari_agree():
    composed = "आगे बढ़ते हैं"
    decomposed = "आगे बढ़ते हैं"
    precomposed = "आगे ब\u095dते हैं"
    assert _got(composed) == _got(decomposed) == _got(precomposed) == "change_topic"


def test_zero_width_joiners_are_ignored():
    assert _got("इंटरव्यू‍ यहीं खत्म करते हैं") == "end"


@pytest.mark.parametrize(
    "text",
    [
        "don't move on",
        "please do not repeat",
        "aage mat badhiye",
    ],
)
def test_negated_phrases_do_not_fire(text):
    assert _got(text) == NONE


def test_polite_conditional_is_still_a_request():
    assert _got("if possible, can we move on") == "change_topic"
    assert _got("agar ho sake to aage badhte hain") == "change_topic"


def test_punctuation_ends_the_context_window():
    assert _got("The government failed. Aage badhiye sir") == "change_topic"
    assert _got("The government failed aage badhiye") == NONE


def test_rules_reach_the_same_kind_in_any_case_and_punctuation():
    assert _got("CAN WE MOVE ON???") == _got("can we move on") == "change_topic"
    assert _got("Phir-se boliye!") == "repeat"


# ---------------------------------------------------------------------------
# P1.1 (CTO_NOTES_09): the third-person guard must not block the refused topic (M1)
# ---------------------------------------------------------------------------


def _form_values() -> list[str]:
    form = json.loads(SAMPLE_FORM.read_text(encoding="utf-8"))
    values: list[str] = []
    for value in form.values():
        values.extend(value if isinstance(value, list) else [value])
    return values


def test_public_administration_is_refused_by_name():
    hit = detect_request("public administration ke alawa kisi aur topic pe baat karte hain")
    assert hit is not None
    assert hit.kind == "change_topic"
    assert hit.avoid_terms == ("public administration",)


@pytest.mark.parametrize(
    "text, term",
    [
        ("government ke alawa kisi aur topic pe baat karte hain", "government"),
        ("kisan ke alawa kisi aur topic pe baat karte hain", "kisan"),
        ("students ke alawa kisi aur topic pe baat karte hain", "students"),
        ("foreign ministers ke alawa kuch aur puchiye", "foreign ministers"),
        ("enough about the government", None),
        ("other than government can we talk about something else", "government"),
    ],
)
def test_third_person_nouns_can_be_the_refused_topic(text, term):
    hit = detect_request(text)
    assert hit is not None
    assert hit.kind == "change_topic"
    if term is not None:
        assert hit.avoid_terms == (term,)


@pytest.mark.parametrize("word", sorted(_THIRD_PERSON))
def test_every_third_person_word_can_be_the_refused_topic(word):
    hit = detect_request(f"{word} ke alawa kisi aur topic pe baat karte hain")
    assert hit is not None
    assert hit.kind == "change_topic"
    assert hit.avoid_terms == (word,)


@pytest.mark.parametrize("value", _form_values())
def test_every_sample_form_value_can_be_the_refused_topic(value):
    hit = detect_request(f"{value} ke alawa kisi aur topic pe baat karte hain")
    assert hit is not None
    assert hit.kind == "change_topic"
    assert hit.avoid_terms == (value.lower(),)


@pytest.mark.parametrize(
    "text",
    [
        "government ne kaha kisi aur topic pe baat karte hain",  # reported speech stays guarded
        "mat kisi aur topic pe baat karo",
        "agar kisi aur topic pe baat karte hain to theek hai",
    ],
)
def test_strong_change_keeps_the_negator_and_reported_speech_checks(text):
    assert _got(text) == NONE


def test_third_person_guard_still_blocks_weak_phrases():
    assert _got("The government failed aage badhiye") == NONE


# ---------------------------------------------------------------------------
# P1.1: `end` is the one kind the candidate cannot undo (M2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "campus placements mein hum interview khatam karte hain",
        "company mein hum interview band kar dete hain",
        "hamare yahan interview band karte hain",
        "pradhan mantri ka interview band kijiye",
        "parliament ka session band karo",
        "sansad ka session khatam karte hain",
        "we want to end the session",
        "as the opposition we want to end the session",
    ],
)
def test_describing_an_interview_or_session_is_not_end(text):
    assert _got(text) == NONE


@pytest.mark.parametrize(
    "text",
    [
        "I want to end the session",
        "we want to end the interview",
        "can we end the mock session",
        "let's end the interview here",
        "interview yahin khatam karte hain",
        "sir session band kijiye",
        "इंटरव्यू यहीं खत्म करते हैं",
    ],
)
def test_real_end_requests_still_fire(text):
    assert _got(text) == "end"
