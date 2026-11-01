"""Panel personas and fixed lines (P2, CTO_NOTES_06 §6). Nothing here reaches a session yet."""

from __future__ import annotations

import itertools
import re

import pytest

from mockboard.brain.actor import PLACE_FACT_PATTERNS
from mockboard.brain.agenda import _SEEDS, _WORK_SEEDS, SEGMENT_MEMBERS, SITUATIONS
from mockboard.brain.panel_lines import (
    ALL_LINE_TABLES,
    CAP_CLOSE_LINES,
    CHANGE_TOPIC_ACK_LINES,
    CLARIFY_PANEL_LINES,
    CLOSE_LINES,
    END_CONFIRM_LINES,
    END_DECLINED_LINES,
    HANDOFF_LINES,
    REPEAT_PREFIX_LINES,
    RITUAL_LINES,
    SKIP_ACK_LINES,
    handoff_line,
    line_for,
)
from mockboard.brain.personas import (
    PERSONA_DIR,
    chairman,
    load_personas,
    member_for_topic,
    render_persona_pack,
)
from mockboard.speech.gnani_protocol import TIMBRE_V25_VOICES, TIMBRE_V30_VOICES
from mockboard.voice.lines import split_sentences

PANEL = load_personas(PERSONA_DIR / "panel")
ALL_LINES = [(mix, line) for table in ALL_LINE_TABLES for mix, line in table.items()]
SPOKEN_TEXT = [
    *(line for _, line in ALL_LINES),
    *(text for mixes in _SEEDS.values() for seeds in mixes.values()
      for seed in seeds for text in (seed.template, seed.generic)),
    *_WORK_SEEDS.values(),
    *(text for situation in SITUATIONS for text in situation.values()),
    *(line for p in PANEL.values() for line in (*p.fallback_lines, *p.filler_lines)),
]  # fmt: skip
INFORMAL = re.compile(r"\b(?:tum|tu|tera|tumhara|tumhe)\b|\bतुम|\bतेरा")


def test_panel_is_a_chairperson_and_four_members():
    assert set(PANEL) == set(SEGMENT_MEMBERS.values())
    assert chairman(PANEL).id == "chairman"
    assert sum(p.role == "member" for p in PANEL.values()) == 4


@pytest.mark.parametrize(("segment", "member"), SEGMENT_MEMBERS.items())
def test_each_segment_maps_to_its_member(segment, member):
    assert member_for_topic(PANEL, segment).id == member


def test_old_board_does_not_see_panel_personas():
    assert set(load_personas()) == {"chairman", "economist", "ips_officer"}


@pytest.mark.parametrize("persona", PANEL.values(), ids=lambda p: p.id)
def test_persona_is_complete_and_fictional(persona):
    assert "fictional" in persona.title
    assert persona.domain
    assert len(persona.fallback_lines) >= 2
    assert len(persona.filler_lines) >= 2
    path = PERSONA_DIR / "panel" / f"{persona.id}.yaml"
    assert "Not modelled on any real person" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("persona", PANEL.values(), ids=lambda p: p.id)
def test_voices_are_placeholders_and_candidates_are_in_the_catalog(persona):
    assert persona.voice_id == "PLACEHOLDER_SET_AFTER_TIMBRE_PROBE"
    catalog = set(TIMBRE_V25_VOICES) | set(TIMBRE_V30_VOICES)
    assert persona.voice_candidates
    assert set(persona.voice_candidates) <= catalog


@pytest.mark.parametrize("persona", PANEL.values(), ids=lambda p: p.id)
def test_fallback_lines_state_no_fact_and_no_number(persona):
    for line in persona.fallback_lines + persona.filler_lines:
        assert not re.search(r"\d", line)
        assert not any(re.search(p, line.lower()) for p in PLACE_FACT_PATTERNS)


def test_panel_style_no_longer_asks_for_numbers():
    assert "asks for numbers" not in PANEL["economist"].style.lower()


def test_domain_stays_out_of_the_persona_pack():
    assert PANEL["economist"].domain not in render_persona_pack(PANEL)


# --- Fixed lines -----------------------------------------------------------------------------


@pytest.mark.parametrize("table", ALL_LINE_TABLES)
def test_every_table_has_every_mix(table):
    assert set(table) == {"hi-en", "en", "hi"}
    assert all(line.strip() for line in table.values())


def test_rulings_wording():
    assert line_for(END_CONFIRM_LINES, "hi-en") == "Aap interview yahin samapt karna chahte hain?"
    assert line_for(RITUAL_LINES, "hi-en") == "Namaste. Please be seated."
    assert line_for(CLOSE_LINES, "hi-en") == "Thank you, that will be all."


@pytest.mark.parametrize("mix", ["hi-en", "en", "hi"])
def test_end_confirmation_is_a_question(mix):
    assert line_for(END_CONFIRM_LINES, mix).endswith("?")


@pytest.mark.parametrize("table", [CAP_CLOSE_LINES])
def test_cap_close_is_courteous_and_ends_the_interview(table):
    assert "dhanyavaad" in table["hi-en"].lower()
    assert "thank you" in table["en"].lower()
    assert "धन्यवाद" in table["hi"]


@pytest.mark.parametrize(("mix", "line"), ALL_LINES)
def test_no_line_claims_a_duration_or_a_number(mix, line):
    assert not re.search(r"\d", line)
    low = line.lower()
    for word in ("minute", "minat", "ghant", "hour", "मिनट", "घंट"):
        assert word not in low


@pytest.mark.parametrize("line", SPOKEN_TEXT)
def test_formal_register_only(line):
    """Fixed lines, every seed, situations and persona lines (CTO_NOTES_12 S10)."""
    assert not INFORMAL.search(line.lower())


def test_register_check_reads_the_seeds():
    assert any("Maan lijiye" in line for line in SPOKEN_TEXT)
    assert INFORMAL.search("tum kaise ho")
    assert INFORMAL.search("तुम कैसे हो")


@pytest.mark.parametrize(("mix", "line"), ALL_LINES)
def test_no_gendered_first_person_verb(mix, line):
    assert not re.search(r"\b\w+(?:ta|ti) hoon\b|ता हूँ|ती हूँ", line.lower())


def test_repeat_prefix_is_a_whole_courteous_line():
    assert REPEAT_PREFIX_LINES == {
        "hi-en": "Zaroor, sawal phir se suniye.",
        "en": "Certainly. Here is the question again.",
        "hi": "ज़रूर, प्रश्न फिर से सुनिए।",
    }


@pytest.mark.parametrize("persona", PANEL.values(), ids=lambda p: p.id)
def test_no_filler_promises_a_move(persona):
    """CTO_NOTES_12 S7: a filler plays before the line, and that line may be a probe."""
    assert not any("aage badh" in line.lower() for line in persona.filler_lines)


def test_handoff_names_the_member():
    assert handoff_line("Shrimati Menon", "hi-en") == "Dhanyavaad. Shrimati Menon, please."
    assert all("{name}" in line for line in HANDOFF_LINES.values())


def test_panel_names_are_the_spoken_forms():
    assert {p.name for p in PANEL.values()} == {
        "Chairperson Sharma",
        "Shrimati Menon",
        "Doctor Iyer",
        "Professor Sen",
        "Shri Rathore",
    }


@pytest.mark.parametrize("persona", PANEL.values(), ids=lambda p: p.id)
@pytest.mark.parametrize(("mix", "thanks", "ask"), [
    ("hi-en", "Dhanyavaad.", "{name}, please."),
    ("en", "Thank you.", "{name}, please."),
    ("hi", "धन्यवाद।", "{name}, कृपया।"),
])  # fmt: skip
def test_handoff_stays_whole(persona, mix, thanks, ask):
    """CTO_NOTES_12 M1: a stop in a title made Timbre speak a one-word request, "Smt."."""
    assert split_sentences(handoff_line(persona.name, mix)) == [
        thanks,
        ask.format(name=persona.name),
    ]


def test_unknown_mix_falls_back_to_hinglish():
    assert line_for(CLOSE_LINES, "xx") == CLOSE_LINES["hi-en"]


# --- Variants (Ruling 73 item 3): no fixed line says the same words twice in a row ---------------

REPEATABLE = (
    HANDOFF_LINES, CHANGE_TOPIC_ACK_LINES, SKIP_ACK_LINES, REPEAT_PREFIX_LINES,
    END_CONFIRM_LINES, END_DECLINED_LINES, CLARIFY_PANEL_LINES,
)  # fmt: skip
ALL_WORDINGS = [
    (table.id, mix, text)
    for table in ALL_LINE_TABLES
    for mix, options in table.variants.items()
    for text in options
]
DEVANAGARI = re.compile(r"[\u0900-\u097f]")


@pytest.mark.parametrize("table", REPEATABLE, ids=lambda t: t.id)
def test_a_line_that_can_come_up_twice_has_two_or_three_wordings_in_every_mix(table):
    assert set(table.variants) == {"hi-en", "en", "hi"}
    for options in table.variants.values():
        assert 2 <= len(options) <= 3 and len(set(options)) == len(options)


@pytest.mark.parametrize("table", ALL_LINE_TABLES, ids=lambda t: t.id)
def test_the_first_wording_is_the_one_the_table_has_always_had(table):
    for mix, options in table.variants.items():
        assert table[mix] == options[0] == line_for(table, mix) == line_for(table, mix, 0)


@pytest.mark.parametrize("table", REPEATABLE, ids=lambda t: t.id)
@pytest.mark.parametrize("mix", ["hi-en", "en", "hi"])
def test_no_two_uses_in_a_row_give_the_same_words_even_after_wrapping(table, mix):
    said = [line_for(table, mix, uses) for uses in range(10)]
    assert all(a != b for a, b in itertools.pairwise(said))
    assert said[: len(table.variants[mix])] == list(table.variants[mix])


@pytest.mark.parametrize(("table_id", "mix", "line"), ALL_WORDINGS)
def test_every_wording_keeps_the_register_and_states_nothing(table_id, mix, line):
    assert not re.search(r"\d", line)
    low = line.lower()
    assert not INFORMAL.search(low)
    assert not re.search(r"\b\w+(?:ta|ti) hoon\b|ता हूँ|ती हूँ", low)
    for word in ("minute", "minat", "ghant", "hour", "मिनट", "घंट"):
        assert word not in low
    assert not any(re.search(p, low) for p in PLACE_FACT_PATTERNS)


@pytest.mark.parametrize(("table_id", "mix", "line"), ALL_WORDINGS)
def test_wordings_are_language_aware(table_id, mix, line):
    """Devanagari only in `hi`; the English mix has no Hindi words in Latin script to speak."""
    assert bool(DEVANAGARI.search(line)) == (mix == "hi")
    if mix == "en":
        hindi_in_latin = r"\b(?:aap|kijiye|suniye|dhanyavaad|shukriya|theek|zaroor)\b"
        assert not re.search(hindi_in_latin, line.lower())


@pytest.mark.parametrize("mix", ["hi-en", "en", "hi"])
def test_every_confirmation_wording_is_a_question(mix):
    assert all(text.endswith("?") for text in END_CONFIRM_LINES.variants[mix])


@pytest.mark.parametrize("persona", PANEL.values(), ids=lambda p: p.id)
@pytest.mark.parametrize("mix", ["hi-en", "en", "hi"])
def test_every_handoff_wording_stays_whole_and_names_the_member(persona, mix):
    for uses in range(len(HANDOFF_LINES.variants[mix])):
        line = handoff_line(persona.name, mix, uses)
        assert persona.name in line
        assert len(split_sentences(line)) == 2, line  # a thanks, then the call (CTO_NOTES_12 M1)


def test_clarify_keeps_the_ruling_37_wording_first_and_never_echoes():
    from mockboard.brain.actor import CLARIFY_LINES

    for mix, line in CLARIFY_LINES.items():
        assert CLARIFY_PANEL_LINES.variants[mix][0] == line
    for options in CLARIFY_PANEL_LINES.variants.values():
        assert all("{" not in text for text in options)
