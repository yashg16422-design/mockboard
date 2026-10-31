"""Board lines after the owner's first real voice session (Round 20).

* Ruling 36: topic questions are open-ended; the actor never asks a quiz-style number
  question ("Exampleland mein tourism ka GDP share kya hai?").
* Ruling 37: clarify, pressure and probe lines never repeat the candidate's transcript, which
  can hold speech-recognition errors ("Aapne kuchh badh mein kaha"); clarify asks generically.

The guard runs inside the actor's stream, sentence by sentence, so in voice mode a refused
sentence never reaches the text-to-speech sink.
"""

from __future__ import annotations

import pytest

from mockboard.brain.actor import (
    ACTOR_TEXT_SINK,
    CLARIFY_LINES,
    FILLER_TEMPLATES,
    compose_line,
    quiz_question,
    quotes_answer,
)
from mockboard.brain.prompts import load_prompt
from mockboard.brain.schemas import BoardMove, CandidateForm, Move

OBSERVED_QUIZ = "Exampleland mein tourism ka GDP share kya hai?"
OBSERVED_ASR = "Haan kuchh badh mein bataunga, abhi yaad nahi."


@pytest.mark.parametrize(("sentence", "quiz"), [
    (OBSERVED_QUIZ, True),
    ("Kitne percent log is se jude hain?", True),
    ("How many tourists visit each year?", True),
    ("What is the share of tourism in the economy?", True),
    ("Yeh policy kis saal lagu hui thi?", True),
    ("पर्यटन का प्रतिशत कितना है?", True),
    ("पर्यटन की हिस्सेदारी क्या है?", True),
    ("Tourism se local logon ko kya fayde hote hain, aur kyun?", False),
    ("Is chunauti ko aap kaise suljhayenge?", False),
    ("GDP share par aapki raay kya hai, aur kyun?", False),
    ("Tourism ka GDP share kya hai.", False),  # not a question
])
def test_quiz_questions_are_recognised(sentence, quiz):
    assert quiz_question(sentence) is quiz


@pytest.mark.parametrize(("sentence", "answer", "quoted"), [
    ("Aapne kuchh badh mein kaha, iska matlab kya hai?", OBSERVED_ASR, True),
    ("You said kuchh badh mein, what do you mean?", OBSERVED_ASR, True),
    ("आपने कुछ बाद में कहा, इसका मतलब क्या है?", "कुछ बाद में बताऊँगा", True),
    ("Skilled training kam hai aur repair shops bahut hain, to aap kya karenge?",
     "Repair shops bahut hain, lekin skilled training kam hai aur repair shops bahut hain.", True),
    ("Community policing ke baare mein thoda aur bataiye, aisa kyun?",
     "Community policing aur development dono saath chalne chahiye.", False),
    ("Aap jo keh rahe hain, uska zameeni asar kaise dikhega?", OBSERVED_ASR, False),
])
def test_repeating_the_candidates_words_is_recognised(sentence, answer, quoted):
    assert quotes_answer(sentence, answer) is quoted


def _move(move: Move, target: str | None = None) -> BoardMove:
    return BoardMove(move=move, target=target, member="member_tourism", reason="x: y",
                     rule_id="x")


async def _line(deps, form, move, script, answer=OBSERVED_ASR, sink=None):
    deps.llm.push("actor", script)
    persona = deps.personas.get("member_tourism") or next(iter(deps.personas.values()))
    token = ACTOR_TEXT_SINK.set(sink.append) if sink is not None else None
    try:
        return await compose_line(
            deps.llm, deps.kit, persona, move, topic="tourism", form=form,
            last_question="q", last_answer=answer, max_words=40, max_tokens=200, turn_id="t3",
        )
    finally:
        if token is not None:
            ACTOR_TEXT_SINK.reset(token)


async def test_quiz_move_on_is_replaced_by_an_open_topic_question(deps, form):
    sink: list[str] = []
    line = await _line(deps, form, _move(Move.move_on, "tourism"),
                       f"Theek hai, shukriya. {OBSERVED_QUIZ}", sink=sink)
    assert line.text == ("Theek hai, shukriya. Ab tourism par aate hain: is kshetra mein aapko "
                         "kaunsi chunauti sabse zaroori lagti hai, aur kyun?")
    assert line.guard == "quiz_question" and line.is_fallback
    spoken = "".join(sink)
    assert "Theek hai, shukriya." in spoken and "GDP" not in spoken  # never reached TTS


async def test_probe_that_quotes_the_transcript_becomes_the_template(deps, form):
    sink: list[str] = []
    line = await _line(deps, form, _move(Move.probe),
                       "Aapne kuchh badh mein kaha. Iska matlab kya hai?", sink=sink)
    assert line.text == FILLER_TEMPLATES[Move.probe]
    assert line.guard == "quotes_answer" and sink == []


async def test_a_clean_line_streams_unchanged(deps, form):
    sink: list[str] = []
    text = "Achha. Is par zameen par kya badlav dikhega, aur kyun?"
    line = await _line(deps, form, _move(Move.pressure), text, sink=sink)
    assert line.text == text and not line.is_fallback and not line.guard
    assert "".join(sink).strip() == text


@pytest.mark.parametrize(("language", "mix"), [("hinglish", "hi-en"), ("english", "en"),
                                               ("hindi", "hi")])
async def test_clarify_is_generic_and_never_calls_the_model(deps, language, mix):
    form = CandidateForm(home_state="Uttar Pradesh", degree_subject="Electronics",
                         interview_language=language)
    before = len(deps.llm.calls_named("actor"))
    line = await _line(deps, form, _move(Move.clarify), "Aapne kuchh badh mein kaha?")
    deps.llm._script["actor"].clear()  # the pushed reply was never used
    assert len(deps.llm.calls_named("actor")) == before
    assert line.text == CLARIFY_LINES[mix] and line.guard == "clarify_template"
    assert "kuchh" not in line.text


def test_actor_prompt_v3_states_the_rules_and_has_no_test_data():
    body = load_prompt("actor", 3).body
    assert "open-ended" in body and "not a quiz" in body
    assert "Never repeat the candidate's words" in body
    assert "exampleland" not in body.lower() and "test data" not in body.lower()
