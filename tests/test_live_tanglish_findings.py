"""Regressions from the first real Tanglish sitting (Nov 6, 2026; GENERATED speech).

Timbre (ta-IN, voice Asmita) spoke scripted answers, Prisma v2.5 (ta-IN) transcribed them, and Nova
ran the panel. The transcripts below are Prisma's real output, kept verbatim. They come from
generated speech, so they are evidence about the pipeline, not about human speakers.
"""

from __future__ import annotations

from director_sim import Sim
from mockboard.brain.actor import token_budget
from mockboard.brain.report_panel import heard_turns
from mockboard.brain.requests_ta import detect_tamil_request
from mockboard.brain.schemas import Analysis, BoardMove, CandidateTurn, Move, Segment

# Prisma wrote a spoken "வேறு" (other) as "வேர்" (root).
HEARD_CHANGE_TOPIC = "வேர் தலைப்புக்கு போகலாமா"
# An honest answer that Nova's listener voted `repeat`.
HEARD_HONEST_ANSWER = (
    "எனக்கு சரியாக தெரியாது சார் ஆனால் முதலில் பாதிக்கப்பட்ட மக்களிடம் பேசி பிறகு என்ஜினியர்ஸ் "
    "உடன் திட்டம் போடுவேன்"
)
HEARD_ANSWERS = [
    "எங்கள் மதுரை பகுதியில் குடிநீர் பிரச்சனை மிக அதிகம் நான் பஞ்சாயத்துடன் சேர்ந்து மழைநீர் "
    "சேகரிப்பை கட்டாயமாக்குவேன் ஏனென்றால் நிலத்தடி நீர் குறைந்து வருகிறது",
    "நான் தண்ணீர் விநியோகத்தை முழுவதுமாக தனியாருக்கு கொடுப்பதை ஆதரிக்கவில்லை ஆனால் மெயின்டென்ஸ் "
    "வேலைக்கு ஒரு பிபிபி மாடல் சில இடங்களில் உதவலாம்",
    "நான் முதலில் மெக்கானிக்கல் படித்தேன் சாரி இல்லை சிவில் இன்ஜினியரிங் தான் படித்தேன் டிரைனேஜ் "
    "டிசைன் படிக்கும்போது நகர வெள்ளம் எப்படி வருகிறது என்று புரிந்தது",
    HEARD_HONEST_ANSWER,
    "எங்கள் பள்ளி ஆசிரியர் மீனா அவர்களிடம் இருந்து பொறுமையை கற்றுக்கொண்டேன் கோலம் போடுவதும் "
    "அப்படித்தான் ஒவ்வொரு புள்ளியும் முக்கியம்",
]


def test_prismas_spelling_of_the_topic_change_is_a_request():
    hit = detect_tamil_request(HEARD_CHANGE_TOPIC)
    assert hit is not None and hit.kind.value == "change_topic"


def test_vers_other_meaning_is_not_a_request():
    """"வேர்" is "root": a sentence about roots is never a topic change."""
    assert detect_tamil_request("மரத்தின் வேர் ஆழமாக செல்கிறது, அதனால் நிலத்தடி நீர் முக்கியம்") is None


def test_no_real_answer_from_the_sitting_is_read_as_a_request():
    assert all(detect_tamil_request(text) is None for text in HEARD_ANSWERS)


async def test_an_honest_answer_the_listener_voted_repeat_is_taken_as_an_answer():
    sim = Sim()
    question = sim.director.memory.last_question
    d = await sim.say(HEARD_HONEST_ANSWER, candidate_request="repeat", honest_unknown=True)
    assert d.request_kind is None and d.request_source == "llm_ignored"
    assert d.counted and d.repeat_text != question


async def test_a_short_listener_only_repeat_still_works():
    sim = Sim()
    d = await sim.say("ஒரு முறை கூட", candidate_request="repeat")
    assert d.request_kind == "repeat" and d.request_source == "llm"


async def test_a_long_listener_only_repeat_without_i_dont_know_still_works():
    sim = Sim()
    d = await sim.say("Sir maaf kijiye kya aap woh baat ek baar aur keh denge please",
                      candidate_request="repeat")
    assert d.request_kind == "repeat" and d.request_source == "llm"


def test_tamil_and_hindi_lines_get_room_to_finish():
    """A Tamil line was cut mid-word at the Latin-sized token budget."""
    assert token_budget(120, "ta-en") >= 400 and token_budget(120, "hi") >= 300
    assert token_budget(120, "hi-en") == 120 and token_budget(120, "en") == 120


def test_the_end_confirmation_is_described_as_a_confirmation():
    turn = CandidateTurn(
        turn_id="t7", turn_index=7, topic="interests", question="Q?",
        segments=[Segment(segment_id="s0", segment_index=0, text="நேர்காணலை முடிக்கலாம்")],
        analysis=Analysis(turn_id="t7", word_count=1),
    )
    move = BoardMove(move=Move.clarify, member="chairman", reason="end", rule_id="D-END-CONFIRM",
                     for_turn="t7")
    row = heard_turns([turn, turn.model_copy(update={"turn_id": "t8"})], moves=[move])[0]
    assert row.next_step == "asked you to confirm ending the interview"
