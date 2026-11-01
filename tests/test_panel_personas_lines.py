"""Panel personas and fixed lines (P2, CTO_NOTES_06 §6). Nothing here reaches a session yet."""

from __future__ import annotations

import re

import pytest

from mockboard.brain.actor import PLACE_FACT_PATTERNS
from mockboard.brain.agenda import SEGMENT_MEMBERS
from mockboard.brain.panel_lines import (
    ALL_LINE_TABLES,
    CAP_CLOSE_LINES,
    CLOSE_LINES,
    END_CONFIRM_LINES,
    HANDOFF_LINES,
    RITUAL_LINES,
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

PANEL = load_personas(PERSONA_DIR / "panel")
ALL_LINES = [(mix, line) for table in ALL_LINE_TABLES for mix, line in table.items()]


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


@pytest.mark.parametrize(("mix", "line"), ALL_LINES)
def test_formal_register_only(mix, line):
    assert not re.search(r"\b(?:tum|tu|tera|tumhara|tumhe)\b|\bतुम|\bतेरा", line.lower())


@pytest.mark.parametrize(("mix", "line"), ALL_LINES)
def test_no_gendered_first_person_verb(mix, line):
    assert not re.search(r"\b\w+(?:ta|ti) hoon\b|ता हूँ|ती हूँ", line.lower())


def test_handoff_names_the_member():
    assert handoff_line("Smt. Menon", "hi-en") == "Dhanyavaad. Smt. Menon, please."
    assert all("{name}" in line for line in HANDOFF_LINES.values())


def test_unknown_mix_falls_back_to_hinglish():
    assert line_for(CLOSE_LINES, "xx") == CLOSE_LINES["hi-en"]
