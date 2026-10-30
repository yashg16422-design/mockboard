"""Ruling 33: debate mode's `template_line(move)` hook, which the voice loop calls when a
streamed line is cut short (a guard stop or an actor failure) and nothing said so far asks a
question, so the board still ends with a proper question."""

from __future__ import annotations

import asyncio

import pytest

from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import LLMTransientError
from mockboard.brain.schemas import BoardMove, Move
from mockboard.modes.debate.actor import template_question
from mockboard.modes.debate.guards import split_sentences
from mockboard.modes.debate.motions import Side, load_curated_motions
from mockboard.modes.debate.numbers import mentions_number
from mockboard.modes.debate.session import DebateSetup
from mockboard.modes.debate.strategist import CLOSE_TARGET, DEBATE_MOVES, WARRANT_TARGET
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

MOTIONS = load_curated_motions()
LANGUAGES = {"hinglish": "hi-en", "hindi": "hi", "english": "en"}
SPEECH = "Phones dhyan bhatkate hain kyunki notifications aate hain aur bachche ruk nahi paate."


def mv(move: Move, target: str | None = None, member: str = "opponent") -> BoardMove:
    return BoardMove(move=move, target=target, member=member, reason="x: y", rule_id="x")


def session_for(settings, cards, llm, tmp_path, language="hinglish"):
    deps = build_deps(settings, llm, cards=cards)
    setup = DebateSetup(motion=MOTIONS["smartphones_in_schools"], user_side=Side.for_,
                        language=language)
    return InterviewSession(deps=deps, form=None, recorder=TraceRecorder(tmp_path, "tl"),
                            analyzer=TurnAnalyzer(deps), session_id="tl", mode="debate",
                            mode_options=setup)


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_every_debate_move_has_a_figure_free_closing_sentence(settings, cards, fake_llm,
                                                              tmp_path, language):
    s = session_for(settings, cards, fake_llm, tmp_path, language)
    case = s.hooks.setup.opponent_case
    targets = {Move.cross_question: [case[0].id, WARRANT_TARGET, None],
               Move.move_on: ["closing"]}
    for move in sorted(DEBATE_MOVES, key=lambda m: m.value):
        for target in targets.get(move, [case[0].id]):
            m = mv(move, target)
            text = s.hooks.template_line(m)
            assert text, (move, target)
            assert len(split_sentences(text)) == 1, text  # appended as one sentence
            assert not mentions_number(text), text
            assert s.guard_sentence(m, text), text  # passes the debate's own guards
            if move == Move.closing_statement:
                assert not text.endswith("?")  # the debate ends: a closing line
            else:
                assert text.endswith("?"), (move, text)
            if language == "hindi":
                assert any("ऀ" <= ch <= "ॿ" for ch in text)


def test_the_moderator_closing_the_debate_gets_nothing_appended():
    assert template_question(mv(Move.move_on, CLOSE_TARGET, "moderator"), "hi-en") is None


def test_cross_question_names_the_point_it_presses():
    case = MOTIONS["smartphones_in_schools"].opponent_case(Side.for_)
    q = template_question(mv(Move.cross_question, case[1].id), "en", case)
    assert q == ("What is your answer to this point: parents need to reach children for "
                 "safety and logistics?")
    assert template_question(mv(Move.cross_question, WARRANT_TARGET), "hi-en", case) == (
        "Yeh baat kyun sach hai, aapka tark kya hai?")


async def _ended(s, move: Move, target: str | None):
    await s.open()
    s.submit_segment(SPEECH, segment_index=0)
    chosen = await s.end_of_turn(30)
    s.turn.move = chosen.model_copy(update={"move": move, "target": target})


async def test_guard_stopped_rebut_ends_with_the_debate_question(settings, cards, fake_llm,
                                                                 tmp_path):
    s = session_for(settings, cards, fake_llm, tmp_path)
    await _ended(s, Move.rebut, "A1")
    fake_llm.push("actor", "Aapki baat samajh aati hai. Studies show 40 percent agree. "
                           "Iska jawab kya hai?")
    got = [x async for x in s.speak_stream()]
    assert [g.kind for g in got] == ["model", "question"]
    assert got[-1].text == "Iska jawab kya hai?" and s.turn.question_appended
    assert all("40" not in g.text for g in got)
    s.finish_line()


async def test_off_limits_stop_on_a_cross_question_names_the_point(settings, cards, fake_llm,
                                                                   tmp_path):
    s = session_for(settings, cards, fake_llm, tmp_path, "english")
    await _ended(s, Move.cross_question, "A2")
    fake_llm.push("actor", "You skipped my point. This is just like the BJP line. More text.")
    got = [x async for x in s.speak_stream()]
    assert got[-1].kind == "question"
    assert got[-1].text.startswith("What is your answer to this point: parents need")
    assert all("BJP" not in g.text for g in got)
    s.finish_line()


class FailingAfterOne:
    """The actor sends one sentence, then the stream fails."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def stream_text(self, *, name, **kw):
        if name != "actor":
            return self.inner.stream_text(name=name, **kw)
        return self._stream()

    async def _stream(self):
        yield "Lekin yeh baat itni seedhi nahi hai. "
        await asyncio.sleep(0)
        raise LLMTransientError("stream broke")


async def test_actor_failure_mid_line_ends_with_the_debate_question_in_hindi(
    settings, cards, fake_llm, tmp_path
):
    s = session_for(settings, cards, FailingAfterOne(fake_llm), tmp_path, "hindi")
    await _ended(s, Move.concede_point, "A1")
    got = [x async for x in s.speak_stream()]
    assert got[-1].kind == "question"
    assert got[-1].text == "लेकिन मेरे अगले तर्क पर आप क्या कहेंगे?"
    s.finish_line()
