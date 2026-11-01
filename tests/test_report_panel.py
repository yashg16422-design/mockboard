"""P10: report by member, weakest by code, text-only replay."""

from __future__ import annotations

from mockboard import cli
from mockboard.brain.agenda import SEGMENT_MEMBERS, one_question_agenda
from mockboard.brain.judge import fallback_report
from mockboard.brain.report_panel import (
    DEFAULT_OUTLINE,
    TextReplay,
    decorate_report,
    outline_violations,
    pick_weakest,
    score_turn,
)
from mockboard.brain.schemas import (
    Analysis,
    Balance,
    CandidateTurn,
    PanelRequest,
    ReportLanguage,
    Segment,
    WeakestMoment,
)


def _turn(tid, topic, question, text, *, index=0, **analysis):
    words = len(text.split())
    return CandidateTurn(
        turn_id=tid, turn_index=index, topic=topic, question=question,
        segments=[Segment(segment_id="s0", segment_index=0, text=text)],
        analysis=Analysis(turn_id=tid, word_count=words, **analysis),
    )


def test_garbled_silent_fallback_and_request_turns_are_never_weakest():
    garbled = _turn("t0", "form_hr", "Q?", "asdf qwer", garbled=True)
    silent = _turn("t1", "form_hr", "Q?", "")
    silent.analysis.word_count = 0
    fallback = _turn("t2", "form_hr", "Q?", "something said here", is_fallback=True)
    request = _turn("t3", "form_hr", "Q?", "please repeat that question now",
                    candidate_request="repeat")
    thin = _turn("t4", "degree", "Why electronics?", "I like it.", index=4)
    assert score_turn(garbled, request_ids=set(), pressure=False) is None
    assert score_turn(silent, request_ids=set(), pressure=False) is None
    assert score_turn(fallback, request_ids=set(), pressure=False) is None
    assert score_turn(request, request_ids=set(), pressure=False) is None
    weakest = pick_weakest(
        [garbled, silent, fallback, request, thin],
        requests=[PanelRequest(turn_id="t3", kind="repeat", source="rules")],
    )
    assert weakest is not None and weakest.turn_id == "t4"


def test_honest_unknown_beats_a_full_answer():
    unknown = _turn(
        "t0", "current_affairs", "What issue?", "I do not know yet.",
        honest_unknown=True,
    )
    full = _turn(
        "t1", "degree", "What did you learn?",
        "Electronics taught me to think in systems with inputs and outputs and limits.",
        balance=Balance.balanced, index=1,
    )
    weakest = pick_weakest([unknown, full])
    assert weakest is not None and weakest.turn_id == "t0"


def test_outline_violations_refuse_digits_and_short_lists():
    assert outline_violations(["one."])
    assert outline_violations(list(DEFAULT_OUTLINE)) == []
    assert outline_violations(["Name 12 percent as a fact."])


def test_decorate_report_fills_members_plan_and_form(form):
    report = fallback_report(
        session_id="s", ledger=[], moves=[], language=ReportLanguage.en,
        reason="test", feature_flags={}, prompt_versions={},
    )
    turns = [
        _turn("t0", "form_hr", "Why this service?",
              "I want to serve people in my district with patience."),
        _turn("t1", "degree", "What did you learn?", "I like circuits.", index=1),
    ]
    names = {"chairman": "Chairperson Sharma", "economist": "Doctor Iyer"}
    out = decorate_report(
        report, turns, names=names, segment_members=dict(SEGMENT_MEMBERS), form=form,
    )
    assert len(out.by_member) == 2
    assert out.weakest is not None and out.weakest.turn_id == "t1"
    assert out.practice_plan
    assert out.form == form
    assert any(b.weakest_question for b in out.by_member)


def test_text_replay_compares_word_counts(tmp_path, form):
    moment = WeakestMoment(
        turn_id="t1", segment="degree", question="What did you learn?",
        answer="I like it.", outline=list(DEFAULT_OUTLINE),
    )
    after = _turn(
        "r0", "degree", "What did you learn?",
        "Electronics taught me systems thinking and also its limits in real life.",
        balance=Balance.balanced,
    )
    compared = TextReplay().compare(moment, after)
    assert compared.after_words > compared.before_words
    assert compared.word_gain > 0


def test_one_question_agenda_is_a_single_budget(form):
    agenda = one_question_agenda("degree", "What did you learn from electronics?", form)
    assert agenda.topics == ["degree"] and agenda.total_budget == 1
    assert agenda.segments[0].seeds[0].text.startswith("What did you learn")


def test_cli_replay_sits_the_flagged_question(tmp_path, form, capsys):
    names = {"chairman": "Chairperson Sharma", "economist": "Doctor Iyer"}
    report = decorate_report(
        fallback_report(
            session_id="s", ledger=[], moves=[], language=ReportLanguage.en,
            reason="test", feature_flags={}, prompt_versions={},
        ),
        [_turn("t1", "degree", "What did you learn from electronics?", "I like it.")],
        names=names, segment_members=dict(SEGMENT_MEMBERS), form=form,
    )
    path = tmp_path / "rep.json"
    path.write_text(report.model_dump_json(), encoding="utf-8")
    answers = tmp_path / "a.txt"
    answers.write_text(
        "Electronics taught me to think in systems with inputs, outputs and limits.\n"
    )
    rc = cli.main([
        "--fake", "--replay", str(path), "--script", str(answers), "--no-trace",
        "--session-id", "replay-test",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Replay" in out and "Compare:" in out
    assert "electronics" in out.lower() or "Electronics" in out
