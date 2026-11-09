"""The board's briefing: several sourced sentences, no digit, not a seed, not the interview."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mockboard.brain.affairs import affairs_line, load_affairs, words_heard


def test_the_briefing_is_several_fields_and_the_spoken_part_has_no_digit():
    notes = load_affairs()
    assert {n.field for n in notes} == {"governance", "economy", "work"}
    assert all(n.source_url.startswith("https://") for n in notes)
    spoken = affairs_line(notes)
    assert "Upper Siang" in spoken and "gig" in spoken and "decent work" in spoken
    assert "https://" not in spoken and not any(ch.isdigit() for ch in spoken)
    assert notes[0].as_of not in spoken


def test_prisma_hears_a_note_only_after_the_candidate_names_it():
    assert words_heard("Mera hobby chess hai.") == ()
    assert words_heard("The damage was small.") == ()  # "dam" is not inside another word
    heard = words_heard("I have been following the Siang dam and public consent.")
    assert "Siang" in heard and "Upper Siang" in heard and "gig" not in heard
    assert "social security" in words_heard("Gig workers need social security.")


def test_a_digit_in_the_line_is_refused(tmp_path: Path):
    path = tmp_path / "notes.yaml"
    path.write_text(
        yaml.safe_dump([{
            "id": "bad",
            "as_of": "2026-11-02",
            "field": "economy",
            "words": ["dam"],
            "line": "The dam is 100 metres high.",
            "source": "a paper",
            "source_url": "https://example.com/dam",
        }]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digit"):
        load_affairs(path)


def test_a_missing_file_is_an_empty_brief(tmp_path: Path):
    assert load_affairs(tmp_path / "missing.yaml") == ()
    assert affairs_line(()) == ""
