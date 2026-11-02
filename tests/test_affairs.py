"""The current-affairs note: one sourced sentence, no digit, and it is not a seed."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mockboard.brain.affairs import affairs_line, affairs_words, load_affairs


def test_the_shipped_note_is_one_sentence_with_a_source_and_no_digit():
    notes = load_affairs()
    assert len(notes) == 1
    note = notes[0]
    assert note.id == "siang-upper-2026-11-02"
    assert note.source_url.startswith("https://")
    assert "Siang" in note.line and not any(ch.isdigit() for ch in note.line)
    spoken = affairs_line(notes)
    assert "https://" not in spoken and note.as_of not in spoken
    assert "Siang" in affairs_words(notes)


def test_a_digit_in_the_line_is_refused(tmp_path: Path):
    path = tmp_path / "notes.yaml"
    path.write_text(
        yaml.safe_dump([{
            "id": "bad",
            "as_of": "2026-11-02",
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
