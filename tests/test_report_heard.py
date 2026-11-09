"""How the board heard you: one row per answer, built by code from session state.

It must show the transcript as recognised, only the listener's own-words note (never a copied
run of the transcript), a request in plain words with how it was found, and the next step.
"""

from __future__ import annotations

from mockboard import cli
from mockboard.brain.cards import CardSet
from mockboard.brain.judge import fallback_report
from mockboard.brain.report_panel import HEARD_NOTE, decorate_report, heard_turns, page_fields
from mockboard.brain.schemas import (
    Analysis,
    BoardMove,
    CandidateTurn,
    Move,
    PanelRequest,
    ReportLanguage,
    Segment,
)

MEMBERS = {"form_hr": "Chairperson Sharma", "degree": "Doctor Iyer"}


def _turn(tid, topic, text, *, index=0, question="Aap civil services kyun chahte hain?", **kw):
    return CandidateTurn(
        turn_id=tid, turn_index=index, topic=topic, question=question,
        segments=[Segment(segment_id="s0", segment_index=0, text=text)] if text else [],
        analysis=Analysis(turn_id=tid, word_count=len(text.split()), **kw),
    )


def _move(tid, move, target=None):
    return BoardMove(move=move, member="chairman", reason="test", for_turn=tid, target=target)


def test_each_status_is_named_and_the_next_step_is_plain_words():
    turns = [
        _turn("t0", "form_hr", "Main logon ki seva karna chahta hoon kyunki gaon mein kami dekhi.",
              stance="Wants to serve because of gaps seen in the village", reasons=["Saw gaps"]),
        _turn("t1", "form_hr", "please repeat"),
        _turn("t2", "form_hr", "asdf qwer zxcv", garbled=True),
        _turn("t3", "form_hr", ""),
        _turn("t4", "form_hr", "Mujhe pata nahi, par main seekhunga.", honest_unknown=True,
              stance="Unsure, willing to learn"),
        _turn("t5", "degree", "Electronics ne system thinking sikhaya.", is_fallback=True),
    ]
    moves = [
        _move("t0", Move.probe), _move("t1", Move.repeat_question), _move("t2", Move.clarify),
        _move("t3", Move.reassure), _move("t4", Move.move_on), _move("t5", Move.move_on, "close"),
    ]
    requests = [PanelRequest(turn_id="t1", kind="repeat", source="rules", pattern_id="x")]
    rows = heard_turns(turns, moves=moves, requests=requests, member_of=MEMBERS)
    assert [r.status for r in rows] == [
        "answer", "request", "unclear", "silence", "honest_unknown", "not_analysed",
    ]
    assert rows[0].understood.startswith("Wants to serve")
    assert rows[0].next_step == "asked a follow-up on this answer"
    assert (rows[1].request, rows[1].request_source) == (
        "hear the question again", "the board's phrase rules"
    )
    assert rows[1].understood == "" and rows[1].next_step == "repeated the question"
    assert rows[2].next_step == "asked you to say it again"
    assert rows[4].next_step == "handed over to Doctor Iyer"  # the next turn is another member's
    assert rows[5].next_step == "closed the interview" and rows[5].understood == ""
    assert rows[0].member == "Chairperson Sharma" and rows[5].member == "Doctor Iyer"


def test_a_note_that_copies_the_transcript_is_not_shown_as_understanding():
    text = "Main ITI ke saath tie up karke repair skilling badhaunga gaon mein"
    turn = _turn("t0", "form_hr", text,
                 stance="Main ITI ke saath tie up karke repair skilling badhaunga",
                 reasons=["Proposes skilling through ITI partnerships"])
    row = heard_turns([turn], moves=[_move("t0", Move.probe)], member_of=MEMBERS)[0]
    assert row.understood == "Proposes skilling through ITI partnerships"


def test_a_model_found_request_says_so():
    turn = _turn("t0", "form_hr", "kuch aur poochiye")
    row = heard_turns(
        [turn], requests=[PanelRequest(turn_id="t0", kind="change_topic", source="llm")]
    )[0]
    assert row.request == "change the topic"
    assert "language model" in row.request_source and "not the phrase rules" in row.request_source


def test_long_answers_are_trimmed_with_a_mark():
    text = " ".join(["shabd"] * 80)
    row = heard_turns([_turn("t0", "form_hr", text, stance="x")])[0]
    assert row.said.endswith("…") and len(row.said.split()) == 61


def test_decorate_report_attaches_the_ledger_and_the_page_gets_it_with_its_note():
    turns = [_turn("t0", "form_hr", "Main seva karna chahta hoon.", stance="Wants to serve")]
    base = fallback_report(session_id="s", ledger=[], moves=[_move("t0", Move.move_on, "close")],
                           language=ReportLanguage.en, reason="test",
                           feature_flags={}, prompt_versions={})
    report = decorate_report(
        base, turns, names={"chairman": "Chairperson Sharma"},
        segment_members={"form_hr": "chairman"},
    )
    assert [r.turn_id for r in report.heard] == ["t0"]
    assert report.heard[0].next_step == "closed the interview"
    fields = page_fields(report)
    assert fields["heard"][0]["understood"] == "Wants to serve"
    assert fields["heard_note"] == HEARD_NOTE
    assert "not a mark against you" in HEARD_NOTE


def test_the_cli_report_prints_the_ledger():
    turns = [_turn("t0", "form_hr", "Main seva karna chahta hoon.", stance="Wants to serve")]
    base = fallback_report(session_id="s", ledger=[], moves=[_move("t0", Move.probe)],
                           language=ReportLanguage.en, reason="test",
                           feature_flags={}, prompt_versions={})
    report = decorate_report(base, turns, names={}, segment_members={})
    text = cli.format_report(report, CardSet(cards={}, version="none"))
    assert "How the board heard you:" in text
    assert "understood: Wants to serve" in text
    assert "next: the board asked a follow-up on this answer" in text


def test_the_page_renders_the_ledger_from_text_only_nodes():
    """The page builds the rows with textContent and text nodes, never innerHTML."""
    from pathlib import Path

    app = (Path(__file__).resolve().parents[1] / "src/mockboard/voice/static/app.js").read_text(
        encoding="utf-8"
    )
    start = app.index("function renderHeard")
    body = app[start : app.index("function renderReport")]
    assert "innerHTML" not in body and "renderHeard(grouped, m)" in app


# --- Real-user review fixes in the same report --------------------------------------------------


def test_an_honest_i_do_not_know_is_not_weaker_than_a_thin_one_sided_answer():
    from mockboard.brain.report_panel import pick_weakest
    from mockboard.brain.schemas import Balance

    honest = _turn("t0", "form_hr", "Mujhe exact pata nahi hai, par main local logon se poochunga "
                   "aur pehle samasya samjhunga, phir kadam uthaunga.", honest_unknown=True)
    thin = _turn("t1", "degree", "Haan, technology se sab theek ho jayega.", index=1,
                 balance=Balance.one_sided)
    weakest = pick_weakest([honest, thin])
    assert weakest is not None and weakest.turn_id == "t1"


def test_the_practice_question_drops_the_repeat_line_in_front_of_it():
    from mockboard.brain.panel_lines import REPEAT_PREFIX_LINES
    from mockboard.brain.report_panel import pick_weakest, strip_fixed_lines

    for mix, options in REPEAT_PREFIX_LINES.variants.items():
        for prefix in options:
            question = f"{prefix} Aap pehla qadam kya uthayenge?"
            assert strip_fixed_lines(question) == "Aap pehla qadam kya uthayenge?", (mix, prefix)
    turn = _turn("t0", "form_hr", "Haan.",
                 question="ज़रूर, प्रश्न फिर से सुनिए। Agar aap khud is sthiti mein hote, to kya karte?")
    weakest = pick_weakest([turn])
    assert weakest is not None and weakest.question.startswith("Agar aap khud")


def test_the_practice_plan_keeps_a_place_for_the_hardest_segment():
    from mockboard.brain.report_panel import PLAN_BY_SEGMENT, practice_plan
    from mockboard.brain.schemas import (
        Dimension,
        DimensionScore,
        Evidence,
        SessionReport,
        WeakestMoment,
    )

    scores = [
        DimensionScore(dimension=d, score=3, rationale="r",
                       evidence=[Evidence(turn_id="t0", quote="Main seva karna chahta hoon")])
        for d in Dimension
    ]
    report = SessionReport(session_id="s", scores=scores)
    weakest = WeakestMoment(turn_id="t0", segment="home_district", question="Q?")
    plan = practice_plan(report, weakest)
    assert len(plan) == 3 and plan[-1] == PLAN_BY_SEGMENT["home_district"]
    assert len(practice_plan(report, None)) == 3
