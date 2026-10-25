"""How figures are spoken (spoken.py): the card's own scale, unit words and language mix."""

from __future__ import annotations

import pytest

from mockboard.brain.spoken import challenge_sentence, format_number, spoken_figure, spoken_parts


@pytest.mark.parametrize(
    "value, unit, like, expected",
    [
        # The CTO's case: same quantity, so both sides must come out in the same words.
        (1_500_000, "visitors", "lakh visitors", "15 lakh visitors"),
        (15, "lakh visitors", "lakh visitors", "15 lakh visitors"),
        (15, "lakh visitors", "million visitors", "1.5 million visitors"),
        (1_500_000, "visitors", "million visitors", "1.5 million visitors"),
        (2.5, "crore inr", "lakh inr", "250 lakh rupaye"),
        (5_000_000_000, "inr", "crore inr", "500 crore rupaye"),
        (30, "percent", "percent", "30 percent"),
        (12.5, "%", "percent", "12.5 percent"),
        (37, "districts", "districts", "37 districts"),
        (42, "lakh visitors", None, "42 lakh visitors"),
    ],
)
def test_spoken_figure_uses_the_cards_scale(value, unit, like, expected):
    assert spoken_figure(value, unit, like=like) == expected


def test_large_bare_numbers_get_an_indian_scale_word():
    assert spoken_figure(1_500_000, "visitors") == "15 lakh visitors"
    assert spoken_figure(25_000_000, None) == "2.5 crore"
    assert spoken_figure(4_500, "visitors") == "4500 visitors"  # no separators for TTS


def test_tiny_value_against_a_big_scale_falls_back_to_its_own_scale():
    assert spoken_figure(500, "inr", like="crore inr") == "500 rupaye"


def test_different_base_units_never_share_a_scale():
    assert spoken_figure(30, "percent", like="lakh visitors") == "30 percent"


@pytest.mark.parametrize(
    "mix, expected",
    [("hi-en", "saal 2019"), ("en", "2019"), ("hi", "साल 2019")],
)
def test_dates_are_spoken_as_years(mix, expected):
    assert spoken_figure(2019, "year", is_date=True, mix=mix) == expected


@pytest.mark.parametrize(
    "mix, expected",
    [("hi-en", "15 lakh visitors"), ("en", "15 lakh visitors"), ("hi", "15 लाख visitors")],
)
def test_scale_words_follow_the_language_mix(mix, expected):
    assert spoken_figure(15, "lakh visitors", like="lakh visitors", mix=mix) == expected


def test_parts_split_core_and_noun():
    parts = spoken_parts(15, "lakh visitors")
    assert (parts.core, parts.noun) == ("15 lakh", "visitors")
    assert spoken_parts(12, "percent").core == "12 percent"


@pytest.mark.parametrize("x, s", [(15.0, "15"), (1.5, "1.5"), (2.345, "2.35"), (0.1, "0.1")])
def test_format_number(x, s):
    assert format_number(x) == s


def test_challenge_sentence_shape_and_order():
    text = challenge_sentence(
        claim_figure="25 lakh visitors",
        card_figure="15 lakh visitors",
        card_year=2022,
        source_short="Exampleland test data",
    )
    assert text == (
        "Aapne 25 lakh visitors kaha. Exampleland test data ke 2022 ke figure ke hisaab se "
        "yeh 15 lakh visitors hai. Aapka figure kahan se hai?"
    )


def test_challenge_sentence_is_sentence_cased_even_with_a_lower_case_source():
    text = challenge_sentence(
        claim_figure="30 percent", card_figure="12 percent", card_year=2023,
        source_short="hamare test data",
    )
    assert text.split(". ")[1].startswith("Hamare test data")


def test_challenge_sentence_english():
    text = challenge_sentence(
        claim_figure="30 percent", card_figure="12 percent", card_year=2023,
        source_short="our test data", mix="en",
    )
    assert text == (
        "You said 30 percent. According to our test data for 2023, it is 12 percent. "
        "Where is your figure from?"
    )
