"""The multilingual request set: no negative or adversarial line may be read as a request."""

from __future__ import annotations

import json

from eval.requests.runner import DATA, classify, main


def test_no_false_action_on_negative_or_adversarial_lines(capsys):
    assert main() == 0
    assert "false actions" in capsys.readouterr().out


def test_the_set_covers_every_language_and_group():
    rows = [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line]
    assert {r["lang"] for r in rows} == {"en", "hinglish", "hindi", "tamil", "tanglish"}
    assert {"positive", "negative", "adversarial", "ambiguous"} <= {r["group"] for r in rows}


def test_every_form_hint_phrase_is_in_the_set_and_recognised():
    rows = [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line]
    texts = {r["text"] for r in rows}
    for phrase in ("Kisi aur topic pe baat karte hain.", "कृपया विषय बदलिए।",
                   "வேறு தலைப்புக்கு போகலாமா?", "மீண்டும் சொல்லுங்கள்"):
        assert phrase in texts and classify(phrase) != "none"
