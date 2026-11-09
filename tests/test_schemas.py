"""Schema validation: fact-card integrity, the no-identifier form, tool schemas, data cards."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mockboard.brain.cards import (
    builtin_test_cards,
    card_files,
    load_cards,
    load_cards_or_builtin,
)
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
    form = CandidateForm(
        home_state="Tamil Nadu", degree_subject="Physics", interview_language="tanglish"
    )
    assert form.interview_language == "tanglish"
    with pytest.raises(ValidationError):
        CandidateForm(home_state="UP", degree_subject="Physics", interview_language="kannada")


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
    if not card_files(cards_dir):
        pytest.skip("data/cards not present")
    cs = load_cards(cards_dir)
    assert cs.cards
    for card in cs.cards.values():
        assert card.synthetic_example or card.source_url


def test_load_cards_or_builtin_falls_back(tmp_path):
    cs, origin = load_cards_or_builtin(tmp_path)
    assert origin == "builtin-test-cards" and cs.all_synthetic


def test_cards_are_discovered_from_the_folder_not_from_a_topic_list():
    """P7: no hard-coded topics. The frozen folder holds tourism and internal_security, and a
    third file would be a third topic. `schema.json` is never a topic."""
    from conftest import LOCK_CARDS_DIR

    files = card_files(LOCK_CARDS_DIR)
    assert [p.stem for p in files] == ["internal_security", "tourism"]
    cs = load_cards(LOCK_CARDS_DIR)
    assert {c.topic for c in cs.cards.values()} == {"internal_security", "tourism"}


def test_a_new_topic_file_is_loaded_without_any_code_change(tmp_path):
    import json
    import shutil

    from conftest import LOCK_CARDS_DIR

    for name in ("schema.json", "tourism.json"):
        shutil.copy(LOCK_CARDS_DIR / name, tmp_path / name)
    doc = json.loads((LOCK_CARDS_DIR / "tourism.json").read_text(encoding="utf-8"))
    doc["topic"] = "monuments"
    for n, card in enumerate(doc["cards"]):
        card["id"] = f"test_monuments_{n}"
    (tmp_path / "monuments.json").write_text(json.dumps(doc), encoding="utf-8")
    cs, origin = load_cards_or_builtin(tmp_path)
    assert origin == f"data:{tmp_path}"
    assert {c.topic for c in cs.cards.values()} == {"tourism", "monuments"}
