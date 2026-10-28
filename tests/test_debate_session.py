"""Debate sessions (DB5): rounds through the topics machinery, the case file, the Moderator's
intro and close, fact checks only with the motion's cards, the trace, and the CLI."""

from __future__ import annotations

import json

import pytest

from conftest import answer
from mockboard import cli
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.schemas import Move
from mockboard.config import REPO_ROOT
from mockboard.modes.debate.motions import Side, load_curated_motions
from mockboard.modes.debate.session import DebateSetup, Length, OpponentStyle
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

MOTIONS = load_curated_motions()
OPENING = (
    "Main motion ke paksh mein hoon. Phones class mein dhyan bhatkate hain, kyunki "
    "notifications aur games teacher se zyada attention kheenchte hain."
)
CLASH_STRONG = (
    "Lekin phones learning tools tabhi hain jab sahi use ho, aur bachche khud ko rok nahi paate "
    "kyunki unka self-control abhi ban raha hai. School library aur computer lab yeh kaam bina "
    "distraction ke kar sakte hain, isliye phone ki zaroorat nahi hai."
)
CLASH_PLAIN = (
    "Rule simple hona chahiye kyunki teachers ka time bachta hai aur sab ko pata hota hai."
)
CLOSING = "Closing mein main kahunga ki padhai ka samay padhai ke liye hona chahiye."


def make_session(deps, tmp_path, motion_id="smartphones_in_schools", *, side=Side.for_,
                 length=Length.quick, style=OpponentStyle.logician, language="hinglish",
                 sid="deb", trace=False):
    setup = DebateSetup(motion=MOTIONS[motion_id], user_side=side, length=length,
                        language=language, opponent_style=style)
    return InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(tmp_path, sid, enabled=trace),
        analyzer=TurnAnalyzer(deps), session_id=sid, mode="debate", mode_options=setup,
    )


async def play(session, answers):
    await session.open()
    outs = []
    for text in answers:
        if session.state.finished:
            break
        outs.append(await answer(session, text))
    return outs


async def test_quick_debate_runs_opening_clash_closing(deps, tmp_path):
    s = make_session(deps, tmp_path)
    assert s.state.topics == ["opening", "clash", "closing"]
    intro = await s.open()
    assert "This House would ban smartphones in schools up to Class 10." in intro.text
    assert "Ms. Kapoor" in intro.text and "kaun sa paksh sahi hai is par nahi" in intro.text
    outs = [await answer(s, t) for t in (OPENING, CLASH_STRONG, CLASH_PLAIN, CLOSING)]
    moves = [o.move.move for o in outs]
    assert moves == [Move.opening_statement, Move.concede_point, Move.move_on,
                     Move.closing_statement]
    assert [o.move.rule_id for o in outs] == ["D5", "D8", "D6", "D5"]
    assert s.state.finished
    assert s.hooks.state.conceded_ids == ["A1"]
    assert [b.member for b in s.state.board_lines] == [
        "moderator", "opponent_logician", "opponent_logician", "moderator", "opponent_logician"
    ]
    assert s.closing_line().startswith("The board will confer")


async def test_standard_debate_has_three_clash_exchanges(deps, tmp_path):
    s = make_session(deps, tmp_path, length=Length.standard)
    outs = await play(s, [OPENING] + [CLASH_PLAIN] * 4 + [CLOSING])
    rounds = [r["round"] for r in s.hooks.rows]
    assert rounds == ["opening", "clash", "clash", "clash", "clash", "closing"]
    assert [o.move.move for o in outs][-2:] == [Move.move_on, Move.closing_statement]
    clash_replies = [o.move.member for o in outs[1:4]]
    assert clash_replies == ["opponent"] * 3


async def test_wit_persona_and_english(deps, tmp_path):
    s = make_session(deps, tmp_path, style=OpponentStyle.wit, language="english")
    intro = await s.open()
    assert intro.text.startswith("Welcome to the Mock Board debate.") and "Mr. Menon" in intro.text
    out = await answer(s, OPENING)
    assert s.speaker_name(out.move.member) == "Mr. Menon"


async def test_no_cards_means_no_fact_check_and_no_challenge(deps, tmp_path):
    s = make_session(deps, tmp_path)
    await play(s, [OPENING, "Testland mein tourism share of GDP 30 percent hai in 2023, isliye "
                   "phones ban hone chahiye kyunki focus zaroori hai.", CLASH_PLAIN, CLOSING])
    assert deps.llm.calls_named("librarian") == []
    assert Move.fact_challenge not in [m.move for m in s.state.moves]


async def test_motion_with_cards_fact_checks_and_challenges_a_turn_late(deps, tmp_path):
    # Motion 5 (tourism cards). The built-in test card says 9 percent for 2023.
    s = make_session(deps, tmp_path, "tourism_hill_towns", side=Side.against,
                     length=Length.standard)
    planted = ("Testland mein tourism share of GDP 30 percent hai in 2023, isliye tourism "
               "itna important nahi hai kyunki baaki sectors bade hain.")
    outs = await play(s, [OPENING, planted, CLASH_PLAIN, CLASH_PLAIN, CLASH_PLAIN, CLOSING])
    moves = [o.move for o in outs]
    challenge = next(m for m in moves if m.move == Move.fact_challenge)
    assert challenge.rule_id == "D9" and challenge.member == "opponent"
    assert moves.index(challenge) == 2  # the turn after the claim, never the same turn
    line = outs[2].line.text
    assert line.rstrip().endswith("?") and "9 percent" in line


async def test_trace_lines_carry_the_debate_block(deps, tmp_path):
    s = make_session(deps, tmp_path, trace=True, sid="deb-trace")
    await play(s, [OPENING, CLASH_STRONG, CLASH_PLAIN, CLOSING])
    rows = [json.loads(x) for x in (tmp_path / "deb-trace.jsonl").read_text().splitlines()]
    start = rows[0]
    assert start["event"] == "session_start" and start["mode"] == "debate"
    assert start["motion_id"] == "smartphones_in_schools" and start["user_side"] == "for"
    assert "actor_debate" in start["prompt_versions"]
    turns = [r for r in rows if "turn_id" in r and r.get("event") is None]
    assert [t["debate"]["round"] for t in turns] == ["opening", "clash", "clash", "closing"]
    assert turns[0]["debate_line"]["word_cap"] == 70
    assert turns[-1]["debate_line"]["word_cap"] == 60
    assert turns[0]["speaker"] == "opponent_logician"


async def test_debate_needs_a_setup(deps, tmp_path):
    with pytest.raises(ValueError, match="DebateSetup"):
        InterviewSession(deps=deps, form=None,
                         recorder=TraceRecorder(tmp_path, "x", enabled=False),
                         analyzer=TurnAnalyzer(deps), mode="debate")


def test_cli_fake_debate_runs_end_to_end(tmp_path, capsys):
    rc = cli.main([
        "--mode", "debate", "--fake", "--script", str(REPO_ROOT / "examples" / "fake_debate.txt"),
        "--session-id", "pytest-debate", "--trace-dir", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[Moderator Rao] Namaste, aur Mock Board debate mein swagat hai." in out
    assert "[Ms. Kapoor] (opening_statement, D5)" in out
    assert "(closing_statement, D5)" in out
    assert "[Moderator Rao] The board will confer." in out
    rows = [json.loads(x) for x in (tmp_path / "pytest-debate.jsonl").read_text().splitlines()]
    assert rows[0]["mode"] == "debate" and rows[-1]["event"] == "report"


async def test_voice_hooks_are_mode_agnostic(deps, tmp_path, form):
    from mockboard.brain.schemas import BoardMove

    rebut = BoardMove(move=Move.rebut, member="opponent", reason="D12: x", rule_id="D12")
    s = make_session(deps, tmp_path)
    await s.open()
    assert s.expects_long_turn  # the opening statement comes next
    assert s.guard_sentence(rebut, "Class 10 tak yeh zaroori hai.")
    assert not s.guard_sentence(rebut, "Studies show 40 percent agree.")
    await answer(s, OPENING)
    assert not s.expects_long_turn  # clash
    upsc = InterviewSession(deps=deps, form=form,
                            recorder=TraceRecorder(tmp_path, "u", enabled=False),
                            analyzer=TurnAnalyzer(deps))
    assert upsc.guard_sentence(rebut, "Studies show 40 percent agree.")  # no-op in UPSC
    assert not upsc.expects_long_turn and upsc.closing_line() is None
