"""The request detector (CTO_NOTES_06 section 3) and acceptance A1b over the labelled corpus.

The corpus is synthetic. Its recall says nothing about real Prisma text. No split in it is an
unseen holdout: see the `# corpus history` note at the top of the corpus file.
Run `pytest -s tests/test_requests.py -k corpus` to print the table.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pytest

from mockboard.brain.requests import (
    _COMPILED,
    _THIRD_PERSON,
    _WEAK_CHANGE,
    END_MAX_WORDS,
    SHORT_ANSWER_WORDS,
    TAIL_WORDS,
    ChangeStrength,
    RequestHit,
    RequestKind,
    _priority,
    detect_request,
    word_count,
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
        return [
            json.loads(line) for line in handle if line.strip() and not line.startswith("#")
        ]


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
    _print_table(
        "request corpus, t rows: P1 holdout, SPENT, no longer unseen",
        [r for r in CORPUS if r["split"] == "spent_holdout"],
    )
    _print_table("request corpus, all", CORPUS)

    false_end = [r["id"] for r in CORPUS if r["expect"] != "end" and _got(r["text"]) == "end"]
    assert false_end == []

    negatives = [r for r in CORPUS if r["expect"] == NONE]
    assert [r["id"] for r in negatives if _got(r["text"]) == "change_topic"] == []

    dev = _table([r for r in CORPUS if r["split"] == "dev"])
    for kind, (lines, hit, _false_hits) in dev.items():
        assert hit / lines >= DEV_RECALL_FLOOR, kind


def test_corpus_history_is_labelled_honestly():
    assert CORPUS_PATH.read_text(encoding="utf-8").startswith("# corpus history")
    assert {row["split"] for row in CORPUS} == {"dev", "spent_holdout"}
    earlier = [row for row in CORPUS if row["id"].startswith("h")]
    assert len(earlier) == 75
    assert all(r["split"] == "dev" and r["history"] == "earlier_holdout" for r in earlier)
    spent = [row for row in CORPUS if row["split"] == "spent_holdout"]
    assert len(spent) == 75
    assert all(r["id"].startswith("t") and r["history"] == "p1_holdout_spent" for r in spent)


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


# ---------------------------------------------------------------------------
# P1.1: "X ko chhodiye" and "aage badhte hain" inside ordinary answers (M3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "corruption ko chhodiye asli samasya gareebi hai",
        "reservation ko chhod dete hain to bhi samasya rahegi",
        "tambaku ko chhod dijiye sehat ke liye",
        "भ्रष्टाचार को छोड़िए असली समस्या गरीबी है",
    ],
)
def test_leave_x_aside_inside_an_answer_bans_nothing(text):
    assert detect_request(text) is None


def test_x_ko_chhodiye_as_the_whole_turn_is_a_weak_topic_change():
    hit = detect_request("sir corruption ko chhodiye")
    assert hit is not None
    assert hit.kind == "change_topic"
    assert hit.pattern_id == "change_x_chhod"
    assert hit.avoid_terms == ("corruption",)


def test_x_chhod_never_takes_the_word_sawal():
    hit = detect_request("is sawal ko chhodiye")
    assert hit is not None
    assert hit.kind == "skip"


@pytest.mark.parametrize(
    "text",
    [
        "gaon ke bachche aage badhte hain",
        "bharat aur china aage badhte hain",
        "देश आगे बढ़ते हैं",
        "aage chalte hain aur phir dekhte hain ki kya hota hai",
        f"{FILLER * 3}aur isi tarah gaon ke bachche aage badhte hain",
    ],
)
def test_declarative_aage_inside_an_answer_is_not_a_request(text):
    assert _got(text) == NONE


@pytest.mark.parametrize(
    "text",
    [
        "aage badhte hain",
        "sir aage badhte hain",
        "chaliye aage badhte hain",
        "pata nahi aage badhte hain",
        "agar ho sake to aage badhte hain",
        "sir is par aur nahi bolna, aage badhte hain",
        "aage badhiye",
        "ye mushkil hai aage badhiye sir",
        "aage chaliye",
    ],
)
def test_aage_requests_still_fire(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == "change_aage"


# ---------------------------------------------------------------------------
# P1.1: "next question" asked in Hindi is not "repeat" (M4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, kind",
    [
        ("agla sawal puch lijiye", "skip"),
        ("agla sawal pooch lijiye", "skip"),
        ("agla sawal bata dijiye", "skip"),
        ("next sawal puch lijiye", "skip"),
        ("kuch aur sawal puchiye", "change_topic"),
        ("kisi aur topic ka sawal puchiye", "change_topic"),
    ],
)
def test_asking_for_another_question_is_never_repeat(text, kind):
    assert _got(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "sawal phir se boliye",
        "sawal dobara puchiye",
        "sawal ek baar aur bataiye",
        "sawal dohraiye",
        "sawal repeat kijiye",
    ],
)
def test_true_repeat_of_the_question_still_fires(text):
    assert _got(text) == "repeat"


# ---------------------------------------------------------------------------
# P1.1: priority and pattern strength (M5, Rulings 50 and 53)
# ---------------------------------------------------------------------------

WEAK_LINES = [
    ("aage badhiye", "change_aage"),
    ("sir corruption ko chhodiye", "change_x_chhod"),
    ("can we move on", "change_en_modal"),
    ("lets move on", "change_en_move"),
    ("can we move ahead", "change_en_ahead"),
    ("move on", "change_en_move_bare"),
    ("kuch aur baat karte hain", "change_kuch_baat"),
]


@pytest.mark.parametrize(
    "text, kind",
    [
        ("is sawal ko chhodiye kisi aur topic pe baat karte hain", "change_topic"),
        ("sawal chhodiye, tourism ke alawa kuch aur puchiye", "change_topic"),
        ("can we move on, next question please", "skip"),
        ("aage badhiye, next question please", "skip"),
        ("samajh nahi aaya, phir se boliye", "repeat"),
        ("can we move on, please repeat", "change_topic"),
        ("kuch aur puchiye, I want to end the interview", "end"),
    ],
)
def test_priority_order(text, kind):
    assert _got(text) == kind


def test_rank_order_is_the_ruling():
    ranks = [_priority(pid, kind) for pid, kind, _pattern in _COMPILED]
    assert ranks == sorted(ranks)
    assert sorted(set(ranks)) == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("text, pattern_id", WEAK_LINES)
def test_weak_patterns_say_so(text, pattern_id):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == pattern_id
    assert hit.strength is ChangeStrength.WEAK


def test_every_weak_pattern_has_a_line():
    assert {pattern_id for _text, pattern_id in WEAK_LINES} == _WEAK_CHANGE


def test_the_weak_set_is_the_seven_patterns_of_ruling_56():
    assert {
        "change_aage",
        "change_x_chhod",
        "change_en_modal",
        "change_en_move",
        "change_en_ahead",
        "change_en_move_bare",
        "change_kuch_baat",
    } == _WEAK_CHANGE


@pytest.mark.parametrize(
    "text",
    [
        "tourism ke alawa kisi aur topic pe baat karte hain",
        "kisi aur topic pe baat karte hain",
        "yeh topic chhodiye",
        "can we change the topic",
        "let's talk about something else",
        "something else",
    ],
)
def test_explicit_topic_words_are_strong(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.strength is ChangeStrength.STRONG


@pytest.mark.parametrize(
    "text",
    ["next question please", "please repeat", "what do you mean", "I want to end the interview"],
)
def test_strength_is_only_for_change_topic(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.strength is None


# ---------------------------------------------------------------------------
# P1.1: section 6 should-fixes, and the latency guard (section 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "can we move on from coal by 2050",
        "can we move on from the colonial mindset",
        "we can change the topic of the debate in parliament",
        f"{FILLER * 3}so can we move on from coal by 2050",
        "kuch aur baat karte hain kisanon ki",
        "kisi aur tarah ki baat karte hain sir",
        "dusri baat karte hain ki gaon mein paani nahi hai",
        "दूसरी बात करते हैं कि गांव में पानी नहीं है",
        "koi aur upay par baat karte hain",
    ],
)
def test_section_six_answers_are_not_requests(text):
    assert _got(text) == NONE


@pytest.mark.parametrize(
    "text",
    [
        "can we move on from this",
        "can we move on from tourism",
        "can we move on to agriculture please",
        "can we move on, I have nothing to add",
        "can we change the topic please",
        "can we change the topic, I know little about it",
        "kisi aur topic pe baat karte hain",
        "kisi aur vishay par baat karte hain sir",
        "kisi aur cheez ke bare mein baat karte hain",
        "kuch aur baat karte hain",
        "dusri baat karte hain",
    ],
)
def test_section_six_requests_still_fire(text):
    assert _got(text) == "change_topic"


# ---------------------------------------------------------------------------
# P1.2 (CTO_NOTES_10): a phrase with no topic word cannot be a strong hit (F1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "neta kuch aur baat karte hain aur karte kuch aur hain",
        "chunav mein neta kuch aur baat karte hain",
        "log kuch aur baat karte hain par sach yeh hai",
        "meeting mein kisan kuch aur baat karte hain",
        "debate mein panelists kuch aur baat karte hain",
        f"{FILLER * 4}aur neta kuch aur baat karte hain",
        "students topic badal dete hain",
        "hamare gaon ke log topic chhod dete hain",
        "kisan kisi aur topic pe baat karte hain",
    ],
)
def test_describing_what_others_talk_about_is_not_a_request(text):
    assert _got(text) == NONE


@pytest.mark.parametrize(
    "text",
    [
        "sarkar berozgari ke alawa kuch aur baat karte hain",
        "neta asli mudde ke alawa kuch aur baat karte hain",
    ],
)
def test_alawa_before_the_idiom_is_weak_so_the_listener_can_veto_it(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == "change_kuch_baat"
    assert hit.strength is ChangeStrength.WEAK


@pytest.mark.parametrize(
    "text",
    [
        "kuch aur baat karte hain",
        "hum kuch aur baat karte hain",
        "chaliye kuch aur baat karte hain",
        "sir kuch aur baat karte hain",
        "dusri baat karte hain",
        "yeh sab chhodkar kuch aur baat karte hain",
        "bahut ho gaya, kuch aur baat karte hain",
        "kuch aur baat kijiye",
    ],
)
def test_the_idiom_as_a_request_fires_weak(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == "change_kuch_baat"
    assert hit.strength is ChangeStrength.WEAK


@pytest.mark.parametrize(
    "text",
    ["hum aage badhte hain", "sir hum aage badhte hain"],
)
def test_hum_stays_out_of_the_aage_left_edge(text):
    assert _got(text) == NONE


@pytest.mark.parametrize(
    "text, term",
    [
        ("public service ke alawa kisi aur topic pe baat karte hain", "public service"),
        ("public service ke alawa sir kisi aur topic pe baat karte hain", "public service"),
        ("kisan ko chhodkar kisi aur topic pe baat karte hain", "kisan"),
    ],
)
def test_refused_third_person_topic_stays_strong_with_its_term(text, term):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == "change_other_baat"
    assert hit.strength is ChangeStrength.STRONG
    assert hit.avoid_terms == (term,)


@pytest.mark.parametrize(
    "text",
    [
        "neta ke bare mein nahi, kisi aur topic pe baat kijiye",
        "government ko chhodiye, topic badal dijiye",
        "public kuch aur topic chhodiye",
    ],
)
def test_strong_imperatives_ignore_the_third_person_guard(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.kind == "change_topic"
    assert hit.strength is ChangeStrength.STRONG


# ---------------------------------------------------------------------------
# P1.2: "ask me anything" is the opposite of a topic change (F2, section 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "sir aap kuch bhi puchiye",
        "kuch bhi puchiye",
        "isliye hamein kuch aur chahiye",
        f"{FILLER * 4}isliye hamein kuch aur chahiye",
    ],
)
def test_ask_me_anything_and_we_need_more_fire_nothing(text):
    assert detect_request(text) is None


def test_alawa_kuch_bhi_is_still_a_topic_change():
    hit = detect_request("tourism ke alawa kuch bhi puchiye")
    assert hit is not None
    assert hit.pattern_id == "change_alawa_tail"
    assert hit.avoid_terms == ("tourism",)


def test_kuch_aur_puchiye_still_fires():
    hit = detect_request("kuch aur puchiye")
    assert hit is not None
    assert hit.pattern_id == "change_kuch_aur"


# ---------------------------------------------------------------------------
# P1.2: `end` has no listener behind it, so it leans towards recall (F3)
# ---------------------------------------------------------------------------


def test_dev_end_is_complete_with_no_floor():
    """Exact on purpose: DEV_RECALL_FLOOR hid the loss of h046 at 20 of 21."""
    rows = [r for r in CORPUS if r["split"] == "dev" and r["expect"] == "end"]
    assert len(rows) == 21
    assert [r["id"] for r in rows if _got(r["text"]) != "end"] == []


def test_h046_is_an_end_request():
    row = next(r for r in CORPUS if r["id"] == "h046")
    assert row["text"] == "can we conclude this session"
    hit = detect_request(row["text"])
    assert hit is not None
    assert hit.kind == "end"
    assert hit.pattern_id == "end_en_modal"


@pytest.mark.parametrize(
    "text",
    [
        "ab hum interview khatam karte hain sir",
        "sir hum session yahin band karte hain",
        "can we end the session",
        "could we end this mock session",
        "shall we stop our session",
        "lets finish the session",
        "let us end the session now",
    ],
)
def test_first_person_plural_end_requests_fire(text):
    assert _got(text) == "end"


@pytest.mark.parametrize(
    "text",
    [
        "we want to end the session",
        "as the opposition we want to end the session",
        "we need to end the session",
    ],
)
def test_we_want_plus_bare_session_stays_refused(text):
    assert _got(text) == NONE


# ---------------------------------------------------------------------------
# P1.2: tails, "what exactly", wanting a different topic, skip after a don't-know
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "kisi aur topic par chal sakte hain",
        "kisi aur topic par chalein",
        "koi aur topic le sakte hain",
        "koi aur topic le lete hain",
        "kisi aur vishay par aa sakte hain",
        "किसी और विषय पर चलें",
        "किसी और विषय पर चल सकते हैं",
        "कोई और विषय ले सकते हैं",
        "कोई और विषय ले लेते हैं",
        "किसी और विषय पर आ सकते हैं",
    ],
)
def test_other_topic_verb_tails_are_strong(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == "change_other_topic"
    assert hit.strength is ChangeStrength.STRONG


@pytest.mark.parametrize(
    "text",
    [
        "kisan koi aur topic le sakte hain",
        "kisi aur topic par chalein to log naraz honge",
        "vishay bahut gambhir hai",
    ],
)
def test_other_topic_tails_keep_their_guards(text):
    assert _got(text) == NONE


@pytest.mark.parametrize("text", ["what exactly do you mean", "sorry but what exactly do you mean"])
def test_what_exactly_do_you_mean(text):
    assert _got(text) == "clarify_question"


@pytest.mark.parametrize(
    "text",
    [
        "i would prefer a different topic",
        "I'd prefer a different topic",
        "i'd like another topic",
        "i want some other topic please",
        "can we have a different topic",
        "i would prefer a different subject sir",
    ],
)
def test_wanting_a_different_topic_is_strong(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == "change_en_want"
    assert hit.strength is ChangeStrength.STRONG


@pytest.mark.parametrize(
    "text",
    [
        "i want another question",
        "i would prefer a different topic of research for the students",
        "students said i want a different topic",
    ],
)
def test_change_en_want_stays_on_the_word_topic(text):
    assert _got(text) != "change_topic"


@pytest.mark.parametrize(
    "text",
    [
        "pata nahi sir agla sawal",
        "pata nahi next question",
        "mujhe pata nahi agla sawal",
        "maloom nahi agla sawal",
        "sir i dont know next question",
        "I don't know, next question",
        "no idea sir next question",
        "पता नही अगला सवाल",
    ],
)
def test_skip_after_a_dont_know(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.kind == "skip"


@pytest.mark.parametrize(
    "text",
    [
        "the next question is about water",
        "mujhe pata nahi agla sawal kya hoga",
        "pata nahi ki agla sawal",
        "sarkar ko pata nahi agla sawal",
        "I do not know the next question",
    ],
)
def test_dont_know_needs_the_turn_to_end_on_the_next_question(text):
    assert _got(text) == NONE


@pytest.mark.parametrize(
    "text, kind",
    [
        ("aage badhiye thank you", "change_topic"),
        ("can we move on thank you sir", "change_topic"),
        ("kuch aur baat karte hain thanks", "change_topic"),
        ("next question please thank you", "skip"),
        ("please repeat thanks", "repeat"),
        ("interview yahin khatam karte hain dhanyavaad", "end"),
        ("I want to end the interview, thank you", "end"),
        ("I want to end the interview thank you sir", "end"),
        ("what do you mean shukriya", "clarify_question"),
        ("इंटरव्यू यहीं खत्म करते हैं धन्यवाद", "end"),
        ("आगे बढ़िए थैंक यू", "change_topic"),
    ],
)
def test_thanks_at_the_tail_is_a_filler(text, kind):
    assert _got(text) == kind


@pytest.mark.parametrize(
    "text", ["thank you", "thank you sir", "thanks", "aage badhiye you", "dhanyavaad sir"]
)
def test_thanks_alone_is_nothing_and_you_is_no_filler(text):
    assert _got(text) == NONE


def test_thanks_never_joins_an_avoid_term():
    hit = detect_request("enough about tourism thank you")
    assert hit is not None
    assert hit.avoid_terms == ("tourism",)


# ---------------------------------------------------------------------------
# P1.2: avoid terms behind "key alawa" and "ala wa" (item 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, term",
    [
        ("tourism key alawa kisi aur topic pe baat karte hain", "tourism"),
        ("rural tourism key alawa kuch aur puchiye", "rural tourism"),
        ("tourism ke ala wa kisi aur topic pe baat karte hain", "tourism"),
        ("tourism ke ala wa kuch aur puchiye", "tourism"),
        ("kisan key alawa kisi aur topic pe baat karte hain", "kisan"),
    ],
)
def test_avoid_term_survives_garbled_alawa(text, term):
    hit = detect_request(text)
    assert hit is not None
    assert hit.kind == "change_topic"
    assert hit.avoid_terms == (term,)


def test_key_stays_an_english_word():
    assert _got("the key alawa nothing") == NONE
    assert detect_request("key issue is water") is None


# ---------------------------------------------------------------------------
# P1.2: test hygiene (item 7) and the shared word count (Ruling 56)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, pattern_id",
    [
        ("end the interview", "end_en_bare"),
        ("ye mushkil hai agla sawal puchiye", "skip_agla_tail"),
        ("agla topic par chaliye", "change_agla_topic"),
        ("i would rather not talk about tourism", "change_en_rather"),
        ("i missed it could you say that again", "repeat_tail"),
        ("but what do you mean", "clarify_mean_tail"),
        ("samjhaiye", "clarify_hi_samjhaiye"),
    ],
)
def test_the_untested_patterns_each_win_a_line(text, pattern_id):
    hit = detect_request(text)
    assert hit is not None
    assert hit.pattern_id == pattern_id


@pytest.mark.parametrize(
    "text", ["agla sawal phir se boliye", "agla sawal dohraiye", "dusra sawal dobara puchiye"]
)
def test_an_explicit_again_word_is_repeat_even_after_agla(text):
    assert _got(text) == "repeat"


@pytest.mark.parametrize(
    "text, words",
    [
        ("", 0),
        ("...", 0),
        ("can we move on", 4),
        ("Can we, move on?", 4),
        ("kisiaur topic", 3),  # run together, split
        ("tourism ke a lawa", 3),  # split apart, joined
        ("फिरसे बोलिए", 3),
    ],
)
def test_word_count_counts_as_the_detector_counts(text, words):
    assert word_count(text) == words


def test_the_position_rule_uses_the_same_count():
    """A plain split says 20 words here; the detector says 21, so the mid-turn phrase is out."""
    tail = "the district has good roads and many small farms near the river roads"
    text = f"phir se boliye kisiaur {tail} and the farms"
    assert len(text.split()) == 20
    assert word_count(text) == SHORT_ANSWER_WORDS + 1
    assert detect_request(text) is None
    assert _got(text.replace("kisiaur", "kisi")) == "repeat"


# ---------------------------------------------------------------------------
# F4 (CTO_NOTES_11, Ruling 58): "cheez" means thing, not topic, so it needs an imperative
# ---------------------------------------------------------------------------

_LONG_ANSWER = "gaon mein sadak aur bijli ki halat kaafi kharab hai sir "


@pytest.mark.parametrize(
    "ending",
    [
        "isliye hamein koi aur cheez chahiye",
        "chahe paani ho ya koi aur cheez",
        "pehli cheez shiksha hai aur dusri cheez",
        "subsidy ki jagah hum koi aur cheez le sakte hain",
        "ab hum dusri cheez par aa sakte hain",
    ],
)
def test_cheez_closing_an_answer_is_not_a_request(ending):
    assert _got(_LONG_ANSWER + ending) == NONE


@pytest.mark.parametrize(
    "text",
    [
        "koi aur cheez puchiye",
        "dusri cheez bataiye",
        "kisi aur cheez ke bare mein baat karte hain",
    ],
)
def test_cheez_with_an_imperative_is_a_strong_request(text):
    hit = detect_request(text)
    assert hit is not None
    assert hit.kind is RequestKind.CHANGE_TOPIC
    assert hit.strength is ChangeStrength.STRONG


def test_kuch_baat_mid_sentence_is_not_a_request():
    """Pins change_kuch_baat in _MUST_CLOSE: the phrase does not close its clause here."""
    assert _got("hum kuch aur baat karte hain aur phir ghar jaate hain") == NONE


@pytest.mark.parametrize(
    "token", ["sir", "ko", "kisi aur", "please", "topic", "can we", "ko chhod"]
)
def test_long_repetition_stays_fast(token):
    text = " ".join([token] * 5000)
    started = time.perf_counter()
    detect_request(text)
    assert time.perf_counter() - started < 0.1
