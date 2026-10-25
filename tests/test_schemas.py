"""Schema validation: fact-card integrity, the no-identifier form, tool schemas, data cards."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mockboard.brain.cards import builtin_test_cards, load_cards, load_cards_or_builtin
from mockboard.brain.llm import ToolSpec, assert_flat_schema
from mockboard.brain.schemas import CandidateForm, FactCard
from mockboard.brain.tools import ALL_TOOLS, ListenerOutput
from mockboard.config import REPO_ROOT


def test_unsourced_card_must_be_flagged_synthetic():
    with pytest.raises(ValidationError):
        FactCard(id="x", topic="t", statement="s")
    with pytest.raises(ValidationError):
        FactCard(id="x", topic="t", statement="s", synthetic_example=True, source_url="https://e")
    FactCard(id="x", topic="t", statement="s", synthetic_example=True)


def test_builtin_cards_are_all_synthetic_and_unsourced():
    cs = builtin_test_cards()
    assert cs.cards and cs.all_synthetic
    assert all(c.source_url is None and "TEST CARD" in c.statement for c in cs.cards.values())


def test_form_collects_no_identifiers():
    fields = set(CandidateForm.model_fields)
    assert fields == {
        "home_state",
        "district",
        "degree_subject",
        "hobbies",
        "optional_subject",
        "work_experience",
        "interview_language",
    }
    for forbidden in ("name", "email", "phone", "roll_number", "college"):
        with pytest.raises(ValidationError):
            CandidateForm(home_state="UP", degree_subject="Physics", **{forbidden: "x"})


def test_form_limits():
    with pytest.raises(ValidationError):
        CandidateForm(home_state="UP", degree_subject="Physics", hobbies=["a", "b", "c", "d"])
    with pytest.raises(ValidationError):
        CandidateForm(home_state="UP", degree_subject="Physics", interview_language="tamil")


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_tool_schemas_are_flat_and_match_models(name):
    tool, model = ALL_TOOLS[name]
    assert_flat_schema(tool.input_schema)
    assert set(tool.input_schema["properties"]) == set(model.model_fields)


def test_nested_or_union_schemas_are_rejected():
    with pytest.raises(ValueError):
        ToolSpec("t", "d", {"type": "object", "properties": {}, "$defs": {}})
    with pytest.raises(ValueError):
        ToolSpec("t", "d", {"type": "object", "properties": {"a": {"anyOf": []}}})
    with pytest.raises(ValueError):
        ToolSpec("t", "d", {"type": "object", "properties": {"a": {"type": ["string", "null"]}}})


def test_listener_blank_optional_fields_become_none():
    out = ListenerOutput.model_validate(
        {
            "claims": [
                {
                    "text": "a",
                    "claim_en": "a",
                    "kind": "numeric",
                    "value": "",
                    "unit": " ",
                    "year": "",
                }
            ]
        }
    )
    assert out.claims[0].value is None and out.claims[0].unit is None


def test_data_cards_load_if_present():
    """Builder 2's data/cards/*.json must validate (schema.json + FactCard)."""
    cards_dir = REPO_ROOT / "data" / "cards"
    topics = ["tourism", "internal_security"]
    if not all((cards_dir / f"{t}.json").exists() for t in topics):
        pytest.skip("data/cards not present")
    cs = load_cards(cards_dir, topics)
    assert cs.cards
    for card in cs.cards.values():
        assert card.synthetic_example or card.source_url


def test_load_cards_or_builtin_falls_back(tmp_path):
    cs, origin = load_cards_or_builtin(tmp_path, ["tourism"])
    assert origin == "builtin-test-cards" and cs.all_synthetic
