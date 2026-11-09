"""P5: the panel listener (tool, prompt, vote), how the director weighs it against the rules
(Rulings 53, 56, 57), and the quality of the notes the board remembers (CTO_NOTES_06 §3, §5).

Everything runs on the fake LLM. The fake listener's word lists are not the detector's, so a request
only the listener hears can be shown without touching the frozen `brain/requests.py`."""

from __future__ import annotations

import hashlib
import json

import pytest

from conftest import answer
from director_sim import ANSWER, FORM, Sim
from mockboard import cli
from mockboard.brain.agenda import build_fallback_agenda
from mockboard.brain.director import (
    COPY_RUN_WORDS,
    LLM_REQUEST_MAX_WORDS,
    UNKNOWN_NOTE,
    VETO_OVER_WORDS,
    copies_speech,
    decide_panel,
    fold_turns,
    opening_memory,
)
from mockboard.brain.fake_responders import listener_responder, make_fake_llm
from mockboard.brain.listener import build_listener_user, to_analysis
from mockboard.brain.prompts import PROMPT_DIR, load_prompt
from mockboard.brain.requests import _WEAK_CHANGE, detect_request, word_count
from mockboard.brain.schemas import Analysis, CompletedTurn, Segment
from mockboard.brain.strategist import merge_analyses
from mockboard.brain.tools import (
    ALL_TOOLS,
    CANDIDATE_REQUESTS,
    DEBATE_LISTENER_TOOL,
    LISTENER_TOOL,
    PANEL_LISTENER_TOOL,
    ListenerOutput,
    PanelListenerOutput,
)
from test_panel_invariants import PLAIN, DeadListenerLLM, start, texts

LISTENER_TOOL_MD5 = "c2f8df042080e94ccf34f522485cb7f6"  # json.dumps(input_schema, sort_keys=True)
WEAK_LINES = [
    ("aage badhiye", "change_aage"),
    ("sir corruption ko chhodiye", "change_x_chhod"),
    ("can we move on", "change_en_modal"),
    ("lets move on", "change_en_move"),
    ("can we move ahead", "change_en_ahead"),
    ("move on", "change_en_move_bare"),
    ("kuch aur baat karte hain", "change_kuch_baat"),
]
LEAD = "Maine pehle bhi yeh socha tha aur ab bhi lagta hai"  # 10 words, so a padded turn is over 8
WIDE = "why don't we discuss something different"  # not a pattern of the frozen detector


def padded(line: str) -> str:
    return f"{LEAD}, {line}"


# Two weak patterns only fire on a turn that is the phrase plus fillers (the detector anchors them
# at the start and the end), so their long turns are made of fillers, as a candidate's would be.
LONG_ONLY_FILLERS = {
    "change_x_chhod": "ji sir thank you so much sir, corruption ko chhodiye please",
    "change_en_move_bare": "thank you sir thank you sir thank you sir, move on",
}


def long_turn_for(line: str, pattern: str) -> str:
    return LONG_ONLY_FILLERS.get(pattern, padded(line))


# --- the tool and the prompt --------------------------------------------------------------------


def test_the_panel_listener_adds_its_fields_and_the_other_tools_are_untouched():
    """`candidate_request` (P5) and `prejudice` (listener.v7, Nov 9)."""
    base = LISTENER_TOOL.input_schema
    panel = PANEL_LISTENER_TOOL.input_schema
    assert panel["properties"].keys() == {*base["properties"], "candidate_request", "prejudice"}
    assert panel["required"] == [*base["required"], "candidate_request", "prejudice"]
    assert "prejudice" not in DEBATE_LISTENER_TOOL.input_schema["properties"]
    assert panel["properties"]["candidate_request"]["enum"] == list(CANDIDATE_REQUESTS)
    assert PANEL_LISTENER_TOOL.name == "record_panel_analysis" != LISTENER_TOOL.name
    assert "candidate_request" not in base["properties"]
    assert "candidate_request" not in DEBATE_LISTENER_TOOL.input_schema["properties"]
    assert hashlib.md5(json.dumps(base, sort_keys=True).encode()).hexdigest() == LISTENER_TOOL_MD5
    assert ALL_TOOLS["listener_panel"] == (PANEL_LISTENER_TOOL, PanelListenerOutput)


def test_the_five_request_kinds_are_the_directors():
    assert set(CANDIDATE_REQUESTS) == {
        "none", "change_topic", "repeat", "clarify_question", "skip", "end",
    }


@pytest.mark.parametrize(
    ("value", "kind"),
    [("change_topic", "change_topic"), ("Change-Topic", "change_topic"), (" SKIP ", "skip"),
     ("something else", "none"), ("", "none"), (None, "none"), (3, "none"), ("none", "none")],
)  # fmt: skip
def test_an_odd_value_from_the_model_is_no_request_and_never_an_error(value, kind):
    out = PanelListenerOutput.model_validate({"candidate_request": value})
    assert out.candidate_request == kind


def test_the_upsc_listener_model_knows_nothing_of_requests():
    assert not hasattr(ListenerOutput(), "candidate_request")
    segment = Segment(segment_id="s0", segment_index=0, text="Mera jawab yeh hai.")
    plain = to_analysis(ListenerOutput(), turn_id="t1", turn_index=1, segment=segment)
    assert plain.candidate_request == "none"
    out = PanelListenerOutput(candidate_request="skip")
    heard = to_analysis(out, turn_id="t1", turn_index=1, segment=segment)
    assert heard.candidate_request == "skip"


def test_listener_v5_preserves_meaning_critical_words_and_v4_stays():
    v5 = load_prompt("listener", 5)
    assert v5.meta["version"] == 5
    body = " ".join(v5.body.split())
    for needle in ("Preserve negation", "final corrected position", "do not guess"):
        assert needle in body
    speech = "Kanpur achchhi jagah hai—actually, achchhi jagah nahi hai."
    user = build_listener_user(
        Segment(segment_id="s1", segment_index=0, text=speech), "Where would you begin?"
    )
    assert speech in user


def test_listener_v6_accepts_tamil_script_and_tanglish_request_examples():
    v6 = load_prompt("listener", 6)
    body = " ".join(v6.body.split())
    assert v6.meta["version"] == 6
    assert "Tamil script" in body and "வேறு தலைப்புக்கு போகலாமா" in body


def test_listener_v4_exists_names_the_field_and_v3_stays():
    v4 = load_prompt("listener", 4)
    assert v4.meta["tool"].startswith("record_panel_analysis") and v4.meta["version"] == 4
    body = " ".join(v4.body.split())
    for needle in ("candidate_request", "change_topic", "skip", "repeat", "clarify_question",
                   "why don't we discuss something different", "Be strict", "own words"):
        assert needle in body, needle
    assert (PROMPT_DIR / "listener.v3.md").exists()
    assert "candidate_request" not in load_prompt("listener", 3).body
    assert (PROMPT_DIR / "listener.v4.md").exists()
    assert "| listener | 4 |" in (PROMPT_DIR / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_upsc_profile_wires_the_panel_listener_prompt_tool_and_model(settings, cards):
    """P7: the panel listener is the UPSC listener. The profile carries the tool and model, and the
    default kit's `listener` prompt is v4, so no per-session wiring is left."""
    from mockboard.brain.deps import build_deps
    from mockboard.modes import get_profile

    deps = build_deps(settings, make_fake_llm(cards), cards=cards)
    assert get_profile(None).listener_tool == (PANEL_LISTENER_TOOL, PanelListenerOutput)
    assert deps.kit.versions["listener"] == settings.prompt_listener_version == 7
    assert "candidate_request" in deps.kit.system("listener")[1].text


# --- merging the segments of one turn -----------------------------------------------------------


def an(request: str = "none", **kw) -> Analysis:
    return Analysis(turn_id="t1", candidate_request=request, **kw)


def test_a_merged_turn_takes_the_last_request_a_healthy_listener_heard():
    merged = merge_analyses([an("skip"), an("none"), an("change_topic"), an("none")])
    assert merged.candidate_request == "change_topic" and not merged.any_fallback


def test_a_failed_segment_never_lends_a_vote_and_is_remembered():
    merged = merge_analyses([an("skip"), an("change_topic", is_fallback=True)])
    assert merged.candidate_request == "skip" and merged.any_fallback and not merged.is_fallback
    alone = merge_analyses([an("change_topic", is_fallback=True)])
    assert alone.candidate_request == "none" and alone.is_fallback and alone.any_fallback


def test_the_default_turn_has_no_request_and_no_failure():
    merged = merge_analyses([an()])
    assert merged.candidate_request == "none" and not merged.any_fallback


# --- the director's weighing: the table of `_resolve_request`, row by row -----------------------


async def decide(text: str, **analysis):
    """The first turn of a fresh panel: its budget is not spent, so a request, and not the end of
    the segment, decides the node."""
    sim = Sim()
    turn, _ = sim.build(text, **analysis)
    return await sim.director.decide_turn(turn)


@pytest.mark.parametrize("text", ["tourism ke alawa kisi aur topic pe baat karte hain"])
async def test_rules_and_listener_agree(text):
    d = await decide(text, candidate_request="change_topic")
    assert (d.request_kind, d.request_source, d.request_disagree) == (
        "change_topic", "rules+llm", False)


async def test_a_strong_rule_hit_stands_when_a_healthy_listener_says_none_and_it_is_traced():
    text = "tourism ke alawa kisi aur topic pe baat karte hain"
    assert word_count(text) > VETO_OVER_WORDS
    d = await decide(text, candidate_request="none")
    assert (d.request_kind, d.request_source, d.request_disagree) == ("change_topic", "rules", True)
    assert d.node == "handoff"


async def test_a_rule_hit_beats_a_different_request_from_the_listener():
    d = await decide("next question please", candidate_request="change_topic")
    assert (d.request_kind, d.request_source, d.request_disagree) == ("skip", "rules", True)


@pytest.mark.parametrize(("line", "pattern"), WEAK_LINES, ids=[p for _, p in WEAK_LINES])
async def test_each_of_the_seven_weak_patterns_is_vetoed_on_a_long_turn(line, pattern):
    text = long_turn_for(line, pattern)
    hit = detect_request(text)
    assert hit is not None and hit.pattern_id == pattern, "the long turn must keep the hit"
    assert word_count(text) > VETO_OVER_WORDS
    d = await decide(text, candidate_request="none")
    assert d.request_kind is None and d.request_source == "rules_vetoed" and d.request_disagree
    assert d.request_pattern == pattern and d.counted and d.node == "next_question"
    assert not d.avoid_terms  # a vetoed hit also loses what it would have refused


def test_the_seven_weak_patterns_are_the_ones_the_veto_covers():
    assert {pattern for _, pattern in WEAK_LINES} == _WEAK_CHANGE and len(_WEAK_CHANGE) == 7


@pytest.mark.parametrize(("line", "pattern"), WEAK_LINES, ids=[p for _, p in WEAK_LINES])
async def test_a_weak_hit_on_a_short_turn_is_not_vetoed(line, pattern):
    assert word_count(line) <= VETO_OVER_WORDS
    d = await decide(line, candidate_request="none")
    assert d.request_kind == "change_topic" and d.request_source == "rules"
    assert d.request_pattern == pattern


async def test_a_weak_hit_the_listener_confirms_stands_on_a_long_turn():
    d = await decide(padded("aage badhiye"), candidate_request="change_topic")
    assert d.request_kind == "change_topic" and d.request_source == "rules+llm"
    assert not d.request_disagree


@pytest.mark.parametrize("failure", [{"is_fallback": True}, {"any_fallback": True}])
async def test_a_failed_listener_never_vetoes(failure):
    d = await decide(padded("aage badhiye"), candidate_request="none", **failure)
    assert d.request_kind == "change_topic" and d.request_source == "rules"
    assert not d.request_disagree  # a listener that failed said nothing at all


async def test_a_strong_hit_is_never_vetoed_by_anything():
    for failure in ({}, {"is_fallback": True}):
        d = await decide("tourism ke alawa kisi aur topic pe baat karte hain",
                         candidate_request="none", **failure)
        assert d.request_kind == "change_topic"


@pytest.mark.parametrize(
    ("kind", "node", "counted", "free"),
    [("change_topic", "handoff", True, False), ("skip", "next_question", True, False),
     ("repeat", "handle_request", False, True),
     ("clarify_question", "handle_request", False, True)],
)  # fmt: skip
async def test_a_request_only_the_listener_heard_is_honoured_like_a_rule_hit(
    kind, node, counted, free
):
    text = f"{WIDE} please"
    assert detect_request(text) is None, "the frozen detector must not know this phrase"
    d = await decide(text, candidate_request=kind)
    assert (d.request_kind, d.request_source, d.request_pattern) == (kind, "llm", "")
    assert d.counted is counted and d.free is free
    if kind == "change_topic":
        assert d.node == "handoff" or d.node == "close"
        assert not d.avoid_terms  # the listener names no term; the segment's own aliases refuse
    assert not d.request_disagree


async def test_an_end_only_the_listener_heard_is_ignored():
    d = await decide("I would like to stop here please", candidate_request="end")
    assert d.request_kind is None and d.request_source == "llm_ignored"
    assert d.counted and not d.pending_end


async def test_a_long_turn_takes_no_request_from_the_listener_alone():
    long_turn = " ".join(["Isme ek aur baat hai"] * 8)
    assert word_count(long_turn) > LLM_REQUEST_MAX_WORDS and detect_request(long_turn) is None
    d = await decide(long_turn, candidate_request="change_topic")
    assert d.request_kind is None and d.request_source == "llm_ignored"


async def test_the_limit_is_thirty_words_inclusive():
    turn = " ".join(["baat"] * LLM_REQUEST_MAX_WORDS)
    assert word_count(turn) == LLM_REQUEST_MAX_WORDS
    d = await decide(turn, candidate_request="skip")
    assert d.request_kind == "skip" and d.request_source == "llm"


async def test_no_hit_and_no_vote_is_an_ordinary_answer():
    d = await decide(ANSWER)
    assert (d.request_kind, d.request_source, d.request_disagree) == (None, "none", False)
    assert d.counted


async def test_a_failed_listener_gives_a_vote_of_nothing_for_a_turn_the_rules_missed():
    d = await decide(f"{WIDE} please", candidate_request="change_topic", is_fallback=True)
    assert d.request_kind is None and d.request_source == "none"


async def test_a_declining_reply_to_the_end_question_logs_no_request():
    sim = Sim()
    await sim.say("interview yahin khatam karte hain")
    turn, _ = sim.build("nahi nahi, aage badhiye", candidate_request="change_topic")
    d = await sim.director.decide_turn(turn)
    assert d.request_kind is None and d.request_source == "none"


def test_the_pure_function_and_the_graph_weigh_the_vote_the_same_way():
    agenda = build_fallback_agenda(FORM)
    sim = Sim()
    turn, _ = sim.build(padded("aage badhiye"), candidate_request="none")
    d = decide_panel(turn, agenda, opening_memory(agenda), sim.settings)
    assert d.request_kind is None and d.request_source == "rules_vetoed"


# --- the whole session: the listener wired in ---------------------------------------------------


async def test_a_panel_session_listens_with_the_panel_tool_and_prompt(
    settings, cards, form, tmp_path
):
    session, _, llm = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0)
    call = llm.calls_named("listener")[0]
    assert call.tool == "record_panel_analysis"
    assert "candidate_request" in call.system
    await session.close()


async def test_the_panel_tool_is_not_used_outside_the_panel(settings, cards, form, tmp_path):
    """Before P7 this ran a plain UPSC session and saw the old tool. Now every UPSC session is the
    panel, so the same invariant is asked of the one other mode: debate keeps its own listener."""
    from mockboard.brain.tools import DEBATE_LISTENER_TOOL
    from mockboard.modes import get_profile

    debate = get_profile("debate")
    assert debate.listener_tool[0] is DEBATE_LISTENER_TOOL
    assert debate.listener_tool[0].name == "record_debate_analysis" != PANEL_LISTENER_TOOL.name
    assert debate.prompt_id("listener") == "listener_debate"


async def test_only_the_listener_hears_a_wider_request_and_the_board_moves_on(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0)
    assert detect_request(WIDE) is None
    before = session.state.topic
    out = await answer(session, WIDE, think_s=0)
    row = json.loads(session.recorder.path.read_text(encoding="utf-8").splitlines()[-1])["panel"]
    assert row["request_source"] == "llm" and row["candidate_request"] == "change_topic"
    assert row["request_pattern"] == "" and row["request_disagree"] is False
    assert session.state.topic != before
    assert texts(out)[0] in {
        "Zaroor, koi baat nahi.", "Jaisa aap kahein, ise yahin rakhte hain.",
        "Bilkul, ise yahin chhod dete hain.",
    }
    report = await session.finish()
    assert any("found by the listener" in n for n in report.notes)
    await session.close()


async def test_a_weak_phrase_inside_a_long_answer_is_vetoed_by_a_healthy_listener(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    before = session.state.topic
    text = padded("aage badhiye")
    await answer(session, text, think_s=0)
    row = json.loads(session.recorder.path.read_text(encoding="utf-8").splitlines()[-1])["panel"]
    assert row["candidate_request"] is None and row["request_disagree"] is True
    assert row["request_source"] == "rules_vetoed" and row["request_pattern"] == "change_aage"
    assert session.state.topic == before  # still in the same segment: it was not a request
    report = await session.finish()
    assert not any("Request at" in n for n in report.notes[:-1])  # not listed as a request
    await session.close()


async def test_with_the_listener_down_the_same_weak_phrase_is_honoured(
    settings, cards, form, tmp_path
):
    llm = DeadListenerLLM(responders=make_fake_llm(cards)._responders)
    session, _, _ = await start(settings, cards, form, tmp_path, llm=llm)
    before = session.state.topic
    await answer(session, padded("aage badhiye"), think_s=0)
    assert session.state.topic != before or session.state.turns_in_topic == 0
    row = json.loads(session.recorder.path.read_text(encoding="utf-8").splitlines()[-1])["panel"]
    assert row["candidate_request"] == "change_topic" and row["request_source"] == "rules"
    await session.close()


def test_the_example_script_where_only_the_listener_catches_it(capsys):
    rc = cli.main(
        ["--fake", "--panel", "--script", "examples/fake_panel_listener.txt", "--no-trace"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Request at t3: change_topic, found by the listener." in out
    assert "Request at t5: repeat, found by the listener." in out


# --- the fake listener is a stand-in with its own, wider lists ----------------------------------


@pytest.mark.parametrize(
    ("said", "kind"),
    [("why don't we discuss something different", "change_topic"),
     ("should we shift to another topic", "change_topic"),
     ("I would rather not answer this one", "skip"),
     ("could you say that again", "repeat"),
     ("could you put that more simply", "clarify_question"),
     ("Mujhe lagta hai local rozgaar badhna chahiye.", "none")],
)  # fmt: skip
def test_the_fake_panel_listener_hears_wider_phrases_than_the_detector(said, kind):
    system = load_prompt("listener", 4).body
    user = f"<question>\nQ?\n</question>\n<candidate_speech>\n{said}\n</candidate_speech>"
    assert listener_responder(system, user)["candidate_request"] == kind


def test_the_upsc_fake_listener_does_not_grow_a_request_field():
    system = load_prompt("listener", 3).body
    user = "<candidate_speech>\nwhy don't we discuss something different\n</candidate_speech>"
    assert "candidate_request" not in listener_responder(system, user)


# --- memory notes: what the board remembers, and what never gets in -----------------------------


def fold_note(analysis: Analysis, raw: str = "", **decision) -> str:
    agenda = build_fallback_agenda(FORM)
    sim = Sim()
    turn, _ = sim.build(raw or ANSWER)
    d = decide_panel(turn, agenda, opening_memory(agenda), sim.settings)
    d = d.model_copy(update=decision)
    memory, _, _ = fold_turns(
        opening_memory(agenda), [CompletedTurn(analysis=analysis, decision=d, raw=raw)]
    )
    return memory.notes[0].gist


def test_a_note_never_holds_a_run_of_the_transcript():
    raw = "Mujhe lagta hai gaon mein sadak banane se rozgaar badhta hai aur log shehar jaate hain"
    copied = "gaon mein sadak banane se rozgaar badhta hai"
    assert copies_speech(copied, raw) and COPY_RUN_WORDS == 6
    note = fold_note(Analysis(stance=copied, reasons=["Roads create local jobs"]), raw)
    assert note == "Roads create local jobs"


def test_a_short_overlap_is_not_a_copy():
    assert not copies_speech("rozgaar badhta hai", "gaon mein rozgaar badhta hai sir")
    assert not copies_speech("roads help jobs", "")


def test_the_copy_check_ignores_case_punctuation_and_spacing():
    raw = "Gaon mein  SADAK banane se, rozgaar badhta hai!"
    assert copies_speech("gaon mein sadak banane se rozgaar badhta hai", raw)


def test_a_devanagari_piece_is_not_a_note():
    note = fold_note(Analysis(stance="गाँव में सड़क से रोज़गार बढ़ता है", reasons=["Jobs follow roads"]))
    assert note == "Jobs follow roads"


def test_a_fallback_or_garbled_analysis_leaves_no_note_at_all():
    assert fold_note(Analysis(is_fallback=True, stance="should not appear")) == ""
    assert fold_note(Analysis(garbled=True, stance="kuchh badh mein", reasons=["x y"])) == ""


@pytest.mark.parametrize("kind", ["skip", "change_topic"])
def test_a_turn_that_was_only_a_request_leaves_no_note(kind):
    note = fold_note(Analysis(stance="Candidate wants to change the topic"), request_kind=kind)
    assert note == ""


def test_a_request_turn_with_real_reasons_keeps_them():
    analysis = Analysis(stance="Wants a new topic", reasons=["Roads create jobs"])
    assert fold_note(analysis, request_kind="change_topic") == "Wants a new topic Roads create jobs"


def test_an_honest_unknown_is_kept_as_a_note_the_next_member_can_use():
    assert fold_note(Analysis(honest_unknown=True)) == UNKNOWN_NOTE
    said = Analysis(honest_unknown=True, stance="Does not know the figure")
    assert fold_note(said) == "Does not know the figure"  # the listener already said so


def test_notes_are_trimmed_deduplicated_and_capped():
    pieces = [" ".join(f"{name}{i}" for i in range(40)) for name in ("a", "b", "c")]
    note = fold_note(Analysis(stance=pieces[0], reasons=pieces[1:]))
    assert len(note.split()) == 30
    assert note.split()[:15] == [f"a{i}" for i in range(15)]  # each piece is trimmed to 15
    assert note.split()[15:] == [f"b{i}" for i in range(15)]
    twice = fold_note(Analysis(stance="Same idea", reasons=["same idea", "Another"]))
    assert twice == "Same idea Another"


async def test_no_note_of_a_whole_session_copies_the_candidates_words(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    said = []
    for text in [*PLAIN, WIDE, *PLAIN]:
        if session.state.finished:
            break
        said.append(text)
        await answer(session, text, think_s=0)
    await session.finish()
    notes = session.hooks.director.memory.notes
    assert notes and any(n.gist for n in notes)
    everything = " ".join(said)
    assert all(not copies_speech(n.gist, everything) for n in notes)
    assert all(n.gist.startswith(("Candidate spoke about", UNKNOWN_NOTE)) or not n.gist
               for n in notes)
    await session.close()
