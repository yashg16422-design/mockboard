"""Listener side of Round 20 (rulings 35 and 37).

* An honest "I don't know" in either script ("nahi pata", "baad mein", "नहीं पता") is read
  from the RAW transcript, so even a transcript the listener calls garbled gets reassure or
  move_on, never pressure or a request to repeat. In the owner's run, "kuch baad mein" was
  transcribed as "kuchh badh mein", judged garbled, and later met with pressure.
* The listener handles Devanagari as well as Latin script (Prisma may switch to hi-IN):
  word counts, the garbled check, honest-unknown markers, balance and numbers.
"""

from __future__ import annotations

import pytest

from conftest import answer
from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import fake_responders
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.listener import honest_unknown_marker, to_analysis, word_count
from mockboard.brain.llm import FakeLLMClient
from mockboard.brain.numbers import canonical_unit, parse_number_phrase
from mockboard.brain.prompts import load_prompt
from mockboard.brain.schemas import Move, Segment
from mockboard.brain.tools import ListenerOutput
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

BACKGROUND = ("Kanpur mein repair shops bahut hain, lekin skilled training kam hai. ITI ke "
              "saath tie-up karke skilling badhaunga.")
NEVER_FOR_HONESTY = {Move.pressure, Move.clarify, Move.counter, Move.fact_challenge,
                     Move.probe, Move.steelman}


@pytest.mark.parametrize(("text", "honest"), [
    ("Mujhe exact pata nahi hai.", True),
    ("Sach kahun to nahi pata sir.", True),
    ("Abhi yaad nahi aa raha.", True),
    ("मुझे इसका सही आँकड़ा नहीं पता।", True),
    ("पता नहीं, सर।", True),
    ("எனக்குத் தெரியாது.", True),
    ("Haan kuchh badh mein", True),  # the owner's run: "kuch baad mein", short deflection
    ("Is par main baad mein bataunga, abhi poori jaankari nahi hai mere paas sir.", True),
    ("इस पर मैं बाद में बताऊँगा।", True),
    ("Pehle training denge, baad mein naukriyan aayengi aur log apne gaon mein hi kaam "
     "karenge kyunki skill badhegi.", False),  # "later" as a sequence, not a deflection
    ("Tourism se rozgaar milta hai lekin pollution bhi badhta hai.", False),
])
def test_honest_unknown_markers_in_both_scripts(text, honest):
    assert honest_unknown_marker(text) is honest


def test_a_garbled_honest_answer_is_honest_not_garbled():
    seg = Segment(segment_id="s0", segment_index=0, text="Haan kuchh badh mein")
    a = to_analysis(ListenerOutput(garbled=True, honest_unknown=False), turn_id="t2",
                    turn_index=2, segment=seg)
    assert a.honest_unknown and not a.garbled


def _misreading_listener(base):
    """Like Nova on the owner's run: calls the deflection garbled and not honest."""
    def listener(system: str, user: str) -> dict:
        out = base(system, user)
        if "badh mein" in user or "नहीं पता" in user:
            out.update(garbled=True, honest_unknown=False, reasons=[], claims=[])
        return out
    return listener


@pytest.mark.parametrize("deflection", ["Haan kuchh badh mein", "जी, मुझे नहीं पता।"])
async def test_misread_deflection_gets_reassure_or_move_on_never_pressure(
    settings, cards, form, tmp_path, deflection
):
    responders = fake_responders(cards)
    responders["listener"] = _misreading_listener(responders["listener"])
    deps = build_deps(settings, FakeLLMClient(responders=responders), cards=cards)
    s = InterviewSession(deps=deps, form=form, recorder=TraceRecorder(tmp_path, "r37"),
                         analyzer=TurnAnalyzer(deps), session_id="r37")
    await s.open()
    out = await answer(s, deflection)  # the first answer: not a segment's last question
    assert out.move.move in (Move.reassure, Move.move_on), out.move
    assert out.move.move not in NEVER_FOR_HONESTY
    assert out.move.rule_id in ("R3", "R3b")


async def test_a_devanagari_interview_is_understood(deps, form, tmp_path):
    s = InterviewSession(deps=deps, form=form, recorder=TraceRecorder(tmp_path, "dev"),
                         analyzer=TurnAnalyzer(deps), session_id="dev")
    await s.open()
    await answer(s, BACKGROUND)
    balanced = ("मुझे लगता है पर्यटन से स्थानीय लोगों को रोज़गार मिलता है। लेकिन पर्यावरण पर भी "
                "असर पड़ता है, इसलिए नियम ज़रूरी हैं।")
    out = await answer(s, balanced)
    a = s.state.turns[-1].analysis
    assert not a.garbled and a.balance.value == "balanced"
    assert a.word_count == len(balanced.split())  # whole Devanagari words, not fragments
    assert out.move.move != Move.clarify
    out = await answer(s, "मुझे नहीं पता।")
    assert out.move.move in (Move.reassure, Move.move_on)


def test_devanagari_words_and_numbers():
    assert word_count("मुझे नहीं पता") == 3
    assert word_count("Mujhe nahi pata") == 3
    assert parse_number_phrase("१५ लाख पर्यटक") == 1_500_000
    assert parse_number_phrase("डेढ़ करोड़") == 15_000_000
    assert parse_number_phrase("३० प्रतिशत") == 30
    assert parse_number_phrase("5 हज़ार करोड़") == 5e10
    assert canonical_unit("प्रतिशत") == (1.0, "percent")
    assert canonical_unit("करोड़ रुपये") == (1e7, "inr")


def test_fake_listener_extracts_devanagari_claims():
    from mockboard.brain.fake_responders import listener_responder

    speech = "इस साल १५ लाख पर्यटक आए।"
    user = f"<question>\nq\n</question>\n<candidate_speech>\n{speech}\n</candidate_speech>"
    out = listener_responder("", user)
    assert [(c["value"], c["unit"]) for c in out["claims"]] == [(1_500_000.0, "visitors")]
    garbled = "<question>\nq\n</question>\n<candidate_speech>\nहै\n</candidate_speech>"
    assert listener_responder("", garbled)["garbled"] is False  # a word, not noise


def test_listener_prompts_cover_devanagari_and_honesty():
    for prompt_id, version in (("listener", 3), ("listener_debate", 2)):
        body = load_prompt(prompt_id, version).body
        assert "Devanagari" in body and "नहीं पता" in body and "baad mein bataunga" in body
        assert "never \"garbled\" because of its script" in body
