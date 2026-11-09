"""Tamil-script and romanised Tanglish requests (`requests_ta`), and the director using them.

The phrase lists are ours and unmeasured on real speech (a fluent Tamil speaker has not reviewed
them). These tests lock the contract: the frozen detector is untouched and goes first, a Tamil
request is acted on, and ordinary Tamil answers, negations and reported speech are not requests.
"""

from __future__ import annotations

import pytest

from director_sim import Sim
from mockboard.brain.director import is_clear_yes
from mockboard.brain.requests import detect_request
from mockboard.brain.requests_ta import detect_tamil_request

POSITIVE = [
    ("வேறு தலைப்புக்கு போகலாமா?", "change_topic"),
    ("வேற topic பேசலாமா", "change_topic"),
    ("தலைப்பை மாற்றலாமா?", "change_topic"),
    ("veru topic ku pogalama", "change_topic"),
    ("மீண்டும் சொல்லுங்கள்", "repeat"),
    ("கேள்வியை மீண்டும் சொல்ல முடியுமா?", "repeat"),
    ("மறுபடியும் கேளுங்க", "repeat"),
    ("thirumba sollunga", "repeat"),
    ("repeat பண்ணுங்க", "repeat"),
    ("கேள்வி புரியவில்லை", "clarify_question"),
    ("எனக்கு புரியல", "clarify_question"),
    ("கொஞ்சம் விளக்க முடியுமா?", "clarify_question"),
    ("அடுத்த கேள்விக்கு போகலாம்", "skip"),
    ("இந்த கேள்வியை விட்டுவிடலாம்", "skip"),
    ("இந்தக் கேள்வி வேண்டாம்", "skip"),
    ("adutha kelvi please", "skip"),
    ("நேர்காணலை முடிக்கலாம்", "end"),
    ("interview-ஐ இங்கே நிறுத்துங்கள்", "end"),
    ("இத்துடன் முடித்துக்கொள்ளலாம்", "end"),
    ("நான் இன்டர்வியூவை முடிக்க விரும்புகிறேன்", "end"),
]

NEGATIVE = [
    # negations: "don't repeat", "don't end"
    "மீண்டும் சொல்ல வேண்டாம், நான் புரிந்துகொண்டேன்",
    "நேர்காணலை முடிக்க வேண்டாம்",
    # "another thing is..." inside an answer
    "வேறு விஷயம் என்னவென்றால் தண்ணீர் பிரச்சினை முக்கியம்",
    # reported speech
    "வேறு தலைப்புக்கு போகலாம் என்று அவர் சொன்னார்",
    "திரும்ப கேள்வி கேட்கிறேன் என்று அவர் சொன்னார்",
    # "people did not understand the scheme" is about a policy, not the question
    "அரசு திட்டம் மக்களுக்கு புரியவில்லை, அதனால் விழிப்புணர்வு தேவை",
    "என் கிராமத்தில் தண்ணீர் பிரச்சினை உள்ளது, அரசு திரும்ப திரும்ப வாக்குறுதி கொடுத்தது",
    "நான் மீண்டும் கிராமத்துக்கு திரும்ப விரும்புகிறேன்",
    "அடுத்த தலைமுறைக்கு கல்வி முக்கியம்",
    "நேர்காணல் முடிந்த பிறகு நான் ஊருக்குப் போவேன்",
    "எனக்கு தெரியாது சார்",
    "",
    # ordinary Hinglish and English answers stay untouched
    "Mera maanna hai ki iske do pehlu hain aur dono par dhyan dena zaroori hai sir.",
    "I would not support that policy, because the costs fall on farmers.",
]


@pytest.mark.parametrize(("text", "kind"), POSITIVE)
def test_tamil_requests_are_found(text: str, kind: str):
    hit = detect_tamil_request(text)
    assert hit is not None and hit.kind.value == kind


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_tamil_answers_negations_and_reports_are_not_requests(text: str):
    assert detect_tamil_request(text) is None


def test_an_end_inside_a_long_answer_is_not_a_request():
    long_answer = " ".join(["தண்ணீர் பிரச்சினை முக்கியம்"] * 6) + " நேர்காணலை முடிக்கலாம்"
    assert detect_tamil_request(long_answer) is None


def test_a_request_in_the_tail_of_a_long_answer_is_found():
    long_answer = " ".join(["தண்ணீர் பிரச்சினை மிக முக்கியமானது"] * 6) + " வேறு தலைப்புக்கு போகலாமா?"
    hit = detect_tamil_request(long_answer)
    assert hit is not None and hit.kind.value == "change_topic"


def test_the_frozen_detector_finds_none_of_these_so_the_layer_is_additive():
    """Ruling 57: requests.py has no Tamil patterns. If it ever gains them, revisit this layer."""
    assert all(detect_request(text) is None for text, _ in POSITIVE if not text.isascii())


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("ஆம்", True), ("ஆமாம், முடிக்கலாம்", True), ("ஆமா", True), ("சரி", True),
        ("aama", True), ("நேர்காணலை முடிக்கலாம்", True),
        ("முடிக்க வேண்டாம்", False), ("இல்லை", False), ("சரி, தொடரலாம்", False),
        ("ஆம்?", False), ("vendam", False),
    ],
)
def test_tamil_clear_yes(reply: str, expected: bool):
    assert is_clear_yes(reply) is expected


async def test_a_tamil_end_request_asks_for_confirmation_then_closes():
    sim = Sim()
    d = await sim.say("நேர்காணலை முடிக்கலாம்")
    assert d.pending_end and not d.finished
    assert d.request_kind == "end"
    d = await sim.say("ஆமாம்")
    assert d.finished and d.close_reason == "end_request"


async def test_a_tamil_topic_change_hands_off():
    sim = Sim()
    await sim.say("Mera maanna hai ki iske do pehlu hain aur dono par dhyan dena zaroori hai sir.")
    await sim.say("Mera maanna hai ki iske do pehlu hain aur dono par dhyan dena zaroori hai sir.")
    d = await sim.say("வேறு தலைப்புக்கு போகலாமா?")
    assert d.request_kind == "change_topic" and d.node == "handoff"


async def test_a_tamil_repeat_is_free_and_repeats_the_question():
    sim = Sim()
    question = sim.director.memory.last_question
    d = await sim.say("மீண்டும் சொல்லுங்கள்")
    assert d.request_kind == "repeat" and d.free and not d.counted
    assert d.repeat_text == question
