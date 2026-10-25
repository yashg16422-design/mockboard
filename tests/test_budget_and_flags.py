"""Prompt-size budget and feature-flag defaults.

The size check is an APPROXIMATION: tokens are estimated as characters / 4. Nova's real
tokenizer may count Hinglish/Devanagari differently, so the default budget (18,000) keeps
headroom under Bedrock's documented 20K-token cached-prompt maximum.
"""

from __future__ import annotations

import pytest

from mockboard.brain.cards import CardSet, card_set_version
from mockboard.brain.llm import approx_tokens
from mockboard.brain.personas import load_personas
from mockboard.brain.prompts import build_shared_prefix, check_prefix_budget
from mockboard.brain.schemas import FactCard
from mockboard.config import Settings


def test_default_budget_is_18000(settings):
    assert settings.cache_prefix_token_budget == 18000


def test_budget_is_configurable(monkeypatch):
    monkeypatch.setenv("CACHE_PREFIX_TOKEN_BUDGET", "1234")
    assert Settings(_env_file=None).cache_prefix_token_budget == 1234


def test_persona_plus_cards_prefix_fits_budget(deps, settings):
    tokens = check_prefix_budget(deps.kit.shared_prefix, settings.cache_prefix_token_budget)
    assert tokens == approx_tokens(deps.kit.shared_prefix)  # chars/4 approximation
    assert tokens <= settings.cache_prefix_token_budget


def test_worst_case_card_count_fits_budget(settings):
    """Plan: 40-60 cards per topic, 2 topics. 120 realistic-length cards must still fit."""
    stmt = (
        "SYNTHETIC: Exampleland foreign tourist arrivals were 42 lakh in calendar year 2022, "
        "per the annual tourism statistics report, provisional figures."
    )
    cards = {
        f"c{i}": FactCard(
            id=f"c{i}",
            topic="tourism" if i % 2 else "internal_security",
            statement=stmt,
            aliases=["Exampleland FTA", "videshi paryatak"],
            value=42,
            unit="lakh visitors",
            year=2022,
            synthetic_example=True,
        )
        for i in range(120)
    }
    prefix = build_shared_prefix(load_personas(), CardSet(cards, card_set_version(cards.values())))
    check_prefix_budget(prefix, settings.cache_prefix_token_budget)


def test_budget_guard_raises_when_exceeded():
    with pytest.raises(ValueError, match="approx"):
        check_prefix_budget("x" * 4004, 1000)


def test_all_feature_flags_default_off(settings):
    flags = settings.feature_flags()
    assert flags == {
        "dual_decode": False,
        "board_interrupt": False,
        "echo_filter": False,
        "prisma_partials": False,
        "second_prompt_vote": False,
    }
    assert settings.langfuse_enabled is False


def test_flags_read_from_environment(monkeypatch):
    monkeypatch.setenv("FEATURE_DUAL_DECODE", "true")
    assert Settings(_env_file=None).feature_dual_decode is True


def test_region_and_model_defaults(settings):
    assert settings.aws_region == "ap-southeast-2"
    assert settings.nova_model_id == "amazon.nova-pro-v1:0"
    assert settings.tts_language == "hi-en"
