"""Debate-board invariants (CTO_NOTES_04 §7, DB7) over scripted and seeded random debates.

The invariant checks live in `eval/debate/invariants.py` (shared with `make eval-debate`):
concede on a strong point, never an uncarded number, no repeated cross-question on the same
point, off-limits speech gets `redirect`, an honest "I don't know" is never pounced on, no
challenge from a fallback verdict, every move in the debate's allowed set (plus: the closing
statement concedes something, steelman at most once).

Three adversarial runs make sure the checks do not pass vacuously: an actor that tries to
slip uncarded figures into its lines, a librarian whose calls fail for some claims (fallback
votes), and an end-of-turn deadline too short for any analysis (deadline fallback moves).
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter

import pytest
from eval.debate.invariants import (
    INVARIANTS,
    PlayedDebate,
    make_setup,
    play_debate,
    violations,
)

from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import fake_responders
from mockboard.brain.llm import FakeLLMClient, LLMThrottledError
from mockboard.brain.schemas import Move
from mockboard.config import REPO_ROOT
from mockboard.modes.debate.motions import load_curated_motions

MOTIONS = load_curated_motions()
FAKE_DEBATE = (REPO_ROOT / "examples" / "fake_debate.txt").read_text().splitlines()[5:]

OPENING = ("Main is motion par apna paksh rakhta hoon, kyunki iska asar seedha logon ki "
           "rozmarra zindagi par padta hai aur yeh baat sabse zaroori hai.")
PLAIN = "Rule simple hona chahiye kyunki teachers ka time bachta hai aur sab ko pata hota hai."
UNWARRANTED = "Yeh policy sabke liye behtar hai aur isse sab log khush rahenge."
THIN = "Haan, theek hai."
HONEST = "Sach kahun to mujhe iska exact pata nahi hai."
QUESTION = "Aap yeh batayiye, kya har kisi ke liye yeh sach mein zaroori hai?"
OFF_LIMITS = "Yeh sab politicians ki galti hai kyunki election ke time sab vaade karte hain."
CONCESSION = "Fair point, aapki baat sahi hai ki kuch fayde hain, lekin mera paksh wahi hai."
GARBLED = "a ? . ! b"
CLOSING = ("Closing mein main kahunga ki mera paksh mazboot hai, kyunki maine har tark ka "
           "jawab diya aur kaaran bhi bataye.")

PLANTED = {
    "tourism": "Testland mein tourism share of GDP {n} percent hai in 2023, isliye tourism ka "
               "asar bada hai kyunki log isi par chalte hain.",
}


def strong(motion_id: str, side: str, arg_index: int) -> str:
    """A long, warranted answer that names one opponent argument's keywords: the offline
    listener marks it strong and as answering that argument."""
    case = make_setup(motion_id, side).opponent_case
    arg = case[arg_index % len(case)]
    k = (arg.keywords * 2)[:2]
    return (
        f"Lekin {k[0]} aur {k[1]} wali baat poori tarah sahi nahi hai, kyunki asal mein "
        "zaroorat alag hai aur iska hal doosre tareeke se bhi nikal sakta hai, isliye yeh tark "
        "utna mazboot nahi hai jitna dikhaya gaya."
    )


SCRIPTS: dict[str, dict] = {
    "fake_debate": {"motion": "smartphones_in_schools", "answers": FAKE_DEBATE},
    "two_strong_points_standard": {
        "motion": "metro_before_flyovers", "side": "against", "length": "standard",
        "answers": [OPENING, "S0", "S1", PLAIN, PLAIN, CLOSING],
    },
    "honest_in_every_round": {
        "motion": "gap_year", "length": "standard",
        "answers": [HONEST, HONEST, PLAIN, HONEST, PLAIN, HONEST],
    },
    "off_limits_in_opening_and_clash": {
        "motion": "board_exams", "side": "against",
        "answers": [OFF_LIMITS, OPENING, OFF_LIMITS, PLAIN, CLOSING],
    },
    "ignores_the_opponent": {
        "motion": "work_from_home", "length": "standard",
        "answers": [OPENING, UNWARRANTED, UNWARRANTED, UNWARRANTED, THIN, CLOSING],
    },
    "questions_and_concessions": {
        "motion": "filter_coffee_vs_chai", "style": "wit", "length": "standard",
        "answers": [OPENING, QUESTION, CONCESSION, QUESTION, CONCESSION, CLOSING],
    },
    "silence_and_garble": {
        "motion": "street_food",
        "answers": ["", GARBLED, OPENING, "", PLAIN, CLOSING],
    },
    "planted_figure_on_a_card_motion": {
        "motion": "tourism_hill_towns", "side": "against", "length": "standard",
        "answers": [OPENING, PLANTED["tourism"].format(n=30), PLAIN, PLAIN, PLAIN, CLOSING],
    },
}


def script_answers(spec: dict) -> list[str]:
    side = spec.get("side", "for")
    out = []
    for text in spec["answers"]:
        if re.fullmatch(r"S\d", text):
            text = strong(spec["motion"], side, int(text[1]))
        out.append(text)
    return out


def random_debate(seed: int) -> tuple[dict, list[str]]:
    rng = random.Random(seed)
    # Every fourth debate is on the motion with tourism cards, so the fact-check path (figures,
    # verdicts, delayed challenges) is exercised as often as the qualitative motions.
    motion_id = "tourism_hill_towns" if seed % 4 == 0 else rng.choice(sorted(MOTIONS))
    spec = {
        "motion": motion_id,
        "side": rng.choice(["for", "against"]),
        "length": rng.choice(["quick", "standard"]),
        "style": rng.choice(["logician", "wit"]),
    }
    topics = MOTIONS[motion_id].card_topics
    kinds = ["strong", "plain", "unwarranted", "thin", "honest", "question", "off_limits",
             "concession", "silence", "garbled", "planted"]
    weights = [4, 3, 2, 1, 2, 2, 1, 2, 1, 1, 2 if "tourism" in topics else 0]
    answers = [rng.choice([OPENING, HONEST, OPENING, strong(motion_id, spec["side"], 0)])]
    for kind in rng.choices(kinds, weights=weights, k=9):
        answers.append({
            "strong": lambda: strong(motion_id, spec["side"], rng.randrange(4)),
            "plain": lambda: PLAIN, "unwarranted": lambda: UNWARRANTED, "thin": lambda: THIN,
            "honest": lambda: HONEST, "question": lambda: QUESTION,
            "off_limits": lambda: OFF_LIMITS, "concession": lambda: CONCESSION,
            "silence": lambda: "", "garbled": lambda: GARBLED,
            "planted": lambda: PLANTED["tourism"].format(n=rng.choice([20, 30, 40])),
        }[kind]())
    return spec, answers


async def play(deps, spec: dict, answers: list[str], sid: str, **kw) -> PlayedDebate:
    setup = make_setup(spec["motion"], spec.get("side", "for"), spec.get("length", "quick"),
                       spec.get("style", "logician"))
    return await play_debate(deps, setup, answers, sid, **kw)


# ---------------------------------------------------------------------------
# Scripted debates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCRIPTS))
async def test_scripted_debates_keep_every_invariant(deps, name):
    spec = SCRIPTS[name]
    p = await play(deps, spec, script_answers(spec), f"dinv-{name}")
    assert p.moves, "debate produced no moves"
    assert violations(p) == {}


async def test_strong_points_win_concessions(deps):
    spec = SCRIPTS["two_strong_points_standard"]
    p = await play(deps, spec, script_answers(spec), "dinv-strong")
    conceded = [m for m in p.moves if m.move == Move.concede_point]
    assert [m.rule_id for m in conceded] == ["D8", "D8"]
    assert len({m.target for m in conceded}) == 2  # two different arguments


async def test_honest_unknown_is_acknowledged_in_every_round(deps):
    spec = SCRIPTS["honest_in_every_round"]
    p = await play(deps, spec, script_answers(spec), "dinv-honest")
    honest = [m for a, m in zip(p.analyses, p.moves, strict=True) if a.honest_unknown]
    assert len(honest) == 4 and all(m.preface == "acknowledge" for m in honest)
    assert {m.rule_id for m in honest} == {"D4"}


async def test_off_limits_gets_the_moderators_redirect_in_any_round(deps):
    spec = SCRIPTS["off_limits_in_opening_and_clash"]
    p = await play(deps, spec, script_answers(spec), "dinv-off")
    redirects = [(r, m) for r, m in zip(p.rounds, p.moves, strict=True)
                 if m.move == Move.redirect]
    assert [r for r, _ in redirects] == ["opening", "clash"]
    assert all(m.member == "moderator" and m.rule_id == "D2" for _, m in redirects)


async def test_ignored_points_are_cross_questioned_once_each(deps):
    spec = SCRIPTS["ignores_the_opponent"]
    p = await play(deps, spec, script_answers(spec), "dinv-ignore")
    crosses = [m for m in p.moves if m.move == Move.cross_question]
    assert crosses, "expected at least one cross-question"
    targets = [m.target for m in crosses if m.target != "warrant"]
    assert len(targets) == len(set(targets))


async def test_planted_figure_is_challenged_with_the_card_and_users_figures_only(deps):
    spec = SCRIPTS["planted_figure_on_a_card_motion"]
    p = await play(deps, spec, script_answers(spec), "dinv-planted")
    i = next(i for i, m in enumerate(p.moves) if m.move == Move.fact_challenge)
    assert i == 2 and "30" in p.lines[i].text and "9" in p.lines[i].text


# ---------------------------------------------------------------------------
# Seeded random debates
# ---------------------------------------------------------------------------

SEEDS = range(40)


@pytest.mark.parametrize("seed", SEEDS)
async def test_random_debates_keep_every_invariant(deps, seed):
    spec, answers = random_debate(seed)
    p = await play(deps, spec, answers, f"dinv-rand-{seed}")
    assert violations(p) == {}, (spec, answers)


async def test_random_debates_reach_every_rule_the_invariants_watch(deps):
    """The invariants above must not pass vacuously: across the seeds, every situation they
    check really happens."""
    seen: Counter = Counter()
    motions = set()
    for seed in SEEDS:
        spec, answers = random_debate(seed)
        motions.add(spec["motion"])
        p = await play(deps, spec, answers, f"dinv-cover-{seed}")
        for a, m in zip(p.analyses, p.moves, strict=True):
            seen[m.move.value] += 1
            seen["honest"] += a.honest_unknown
            seen["off_limits"] += a.off_limits
            seen["strong"] += a.point_strength is not None and a.point_strength.value == "strong"
    for key in ("concede_point", "redirect", "cross_question", "fact_challenge", "rebut",
                "closing_statement", "opening_statement", "move_on", "clarify", "reassure",
                "honest", "off_limits", "strong"):
        assert seen[key] > 0, f"no {key} in {len(SEEDS)} random debates: {dict(seen)}"
    assert len(motions) >= 10


# ---------------------------------------------------------------------------
# Adversarial runs
# ---------------------------------------------------------------------------


def _deps_with(settings, cards, **overrides):
    responders = fake_responders(cards)
    responders.update(overrides)
    return build_deps(settings, FakeLLMClient(responders=responders), cards=cards)


async def test_an_actor_that_invents_figures_is_never_heard(settings, cards):
    """The actor tries to add an uncarded statistic to every line; the number guard must drop
    every one of them before the line is spoken."""
    base = fake_responders(cards)["actor"]
    injected = Counter()

    def lying_actor(system: str, user: str) -> str:
        injected["lines"] += 1
        return base(system, user) + " Studies show 73 percent of experts agree with me."

    deps = _deps_with(settings, cards, actor=lying_actor)
    for seed in range(12):
        spec, answers = random_debate(seed)
        p = await play(deps, spec, answers, f"dinv-lie-{seed}")
        assert violations(p).get("no_uncarded_numbers") is None
        assert not any("73" in line.text for line in p.lines)
    assert injected["lines"] > 20


async def test_no_challenge_from_a_fallback_librarian_verdict(settings, cards):
    """The librarian fails on every attempt for half the planted figures (so those claims get
    fallback votes) and answers the rest. Challenges must come only from real answers,
    checked against the librarian's own record, not against our code's flags."""
    base = fake_responders(cards)["librarian"]
    real_contradicted: set[str] = set()
    failed: Counter = Counter()

    def flaky_librarian(system: str, user: str) -> dict:
        claim = json.loads(re.search(r"<claim>\n(.*?)\n</claim>", user, re.S).group(1))
        if int(claim.get("value") or 0) % 20 == 0:  # 20 and 40 fail; 30 and 50 answer
            failed[claim["claim_en"]] += 1
            raise LLMThrottledError("throttled (test)")
        out = base(system, user)
        if out["verdict"] == "contradicted":
            real_contradicted.add(claim["claim_en"])
        return out

    deps = _deps_with(settings, cards, librarian=flaky_librarian)
    challenges = 0
    # Figures on clash turns 1 and 2, so each could be challenged one turn later (before the
    # round's budget runs out at clash turn 4).
    for n_first, n_second in ((20, 30), (30, 40), (50, 20), (40, 50)):
        spec = {"motion": "tourism_hill_towns", "side": "against", "length": "standard"}
        answers = [OPENING, PLANTED["tourism"].format(n=n_first),
                   PLANTED["tourism"].format(n=n_second), PLAIN, PLAIN, CLOSING]
        p = await play(deps, spec, answers, f"dinv-flaky-{n_first}-{n_second}",
                       real_contradicted=real_contradicted)
        assert violations(p) == {}
        challenges += sum(m.move == Move.fact_challenge for m in p.moves)
        for m in p.moves:
            if m.move == Move.fact_challenge:
                claim = next(c for c in p.ledger if c.id == m.target)
                assert claim.value not in (20, 40)
    assert failed and challenges == 4  # both paths really happened


async def test_deadline_fallback_never_challenges_or_concedes(deps):
    """An end-of-turn deadline too short for any analysis: every move is a deadline fallback,
    and none of them is a challenge, a cross-question or a concession."""
    spec = SCRIPTS["two_strong_points_standard"]
    p = await play(deps, spec, script_answers(spec), "dinv-deadline", deadline_s=0.0)
    fallbacks = [m for m in p.moves if m.rule_id == "FALLBACK"]
    assert fallbacks, "the deadline fallback never fired"
    assert violations(p) == {}


def test_invariant_list_covers_the_design():
    assert set(INVARIANTS) >= {
        "concedes_strong_points", "no_uncarded_numbers", "no_repeated_cross_question",
        "off_limits_redirected", "honest_unknown_not_pounced_on", "no_challenge_from_fallback",
        "moves_in_allowed_set",
    }


def test_the_checks_catch_a_broken_debate():
    """Each check fails on a hand-broken record, so a pass means something."""
    from mockboard.brain.schemas import Analysis, BoardLine, BoardMove, PointStrength

    def mv(move, target=None, member="opponent", rule="D12", preface=None):
        return BoardMove(move=move, target=target, member=member, reason=f"{rule}: x",
                         rule_id=rule, preface=preface)

    analyses = [
        Analysis(word_count=30, point_strength=PointStrength.strong, rebuts=["A1"]),
        Analysis(word_count=5, honest_unknown=True),
        Analysis(word_count=8, off_limits=True),
        Analysis(word_count=8, concedes_point=True),
        Analysis(word_count=8),
    ]
    moves = [
        mv(Move.rebut, "A2"),
        mv(Move.cross_question, "A2"),
        mv(Move.rebut, "A2"),
        mv(Move.cross_question, "A2"),
        mv(Move.probe),
    ]
    lines = [BoardLine(turn_id=f"t{i}", member="opponent", move=m.move,
                       text="Research says 64 percent disagree." if i == 0 else "Theek hai.")
             for i, m in enumerate(moves)]
    p = PlayedDebate(
        motion_id="x", analyses=analyses, moves=moves, lines=lines, intro="Namaste.",
        rounds=["clash"] * 5, last_in_round=[False] * 5,
        standing_before=[["A1", "A2"]] * 5, ledger=[], allowed=frozenset(), rows=[],
    )
    assert set(violations(p)) == {
        "concedes_strong_points", "no_uncarded_numbers", "no_repeated_cross_question",
        "off_limits_redirected", "honest_unknown_not_pounced_on", "moves_in_allowed_set",
    }


# ---------------------------------------------------------------------------
# The eval set (`make eval-debate`)
# ---------------------------------------------------------------------------


def test_eval_debate_set_passes_its_gate(capsys):
    from eval.debate import runner

    items = runner.load_items()
    assert len(items) >= 8 and len({i["id"] for i in items}) == len(items)
    assert {i.get("report_language", "en") for i in items} == {"en", "hi"}
    assert runner.main([]) == 0
    out = capsys.readouterr().out
    assert '"gate_passed": true' in out


def test_eval_debate_gate_fails_on_a_violation():
    from eval.debate.runner import summarize

    row = {"id": "x", "violations": {"off_limits_redirected": ["turn 1"]},
           "report_problems": [], "missing_moves": [], "gated_missing": [],
           "judge_fallback": False}
    assert summarize([row])["gate_passed"] is False
    real_miss = {**row, "violations": {}, "missing_moves": ["redirect"], "gated_missing": []}
    assert summarize([real_miss])["gate_passed"] is True  # reported, not gated, with Nova


async def test_report_checks_catch_a_tampered_report(deps):
    from eval.debate.invariants import report_problems

    spec = SCRIPTS["fake_debate"]
    p = await play(deps, spec, script_answers(spec), "dinv-report", report=True)
    assert report_problems(p) == []
    rep = p.report
    reasons = list(rep.verdict.reasons)
    reasons[0] = reasons[0].model_copy(update={"score": 1})
    p.report = rep.model_copy(update={
        "verdict": rep.verdict.model_copy(update={
            "reasons": reasons, "text": "The motion is clearly right."}),
        "best_line": rep.best_line.model_copy(update={"quote": "Words the user never said."}),
    })
    problems = " | ".join(report_problems(p))
    for expected in ("does not match the rubric", "disclaimer", "takes a side",
                     "not the user's own words"):
        assert expected in problems
