"""Board-behaviour invariants (ENGINEERING.md §4, CTO_NOTES_02 M2).

These tests run WHOLE interviews through `InterviewSession` with the fake LLM and check
properties of the conversation as a whole, not single strategist rows. Each defect found by
watching a session becomes one invariant here:

* honesty is never punished (Defect A): an honest "I don't know" only ever gets reassure or
  move_on, and the first one in a topic gets reassure;
* no pressure immediately after reassure (no kind-then-harsh whiplash);
* pressure is capped: never twice in a row, at most two per topic;
* a conceded error is acknowledged before the next move;
* balanced, substantive answers are not pushed back on, and balanced answers in general
  rarely are.

Besides a few hand-written scripts, a seeded generator builds random interviews from a pool
of answer types, so the invariants are checked on many orderings nobody thought to write.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from mockboard.brain.actor import ACKNOWLEDGE_LINES
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.schemas import Analysis, Balance, BoardLine, BoardMove, Move
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

PUSH_BACK = {Move.counter, Move.steelman, Move.pressure}

# A pool of answer types. Figures are about the fictional Testland (built-in test cards).
ANSWERS: dict[str, list[str]] = {
    "honest_unknown": [
        "Mujhe exact pata nahi hai, but I think coordination improve karni chahiye.",
        "Sach kahun to mujhe nahi pata.",
        "I don't know the exact figure, sir.",
    ],
    "thin": ["Haan, important hai.", "Jobs milti hain.", "Yeh zaroori hai."],
    "one_sided": [
        "Tourism se sirf fayda hi fayda hai, isliye government ko har jagah resorts banane chahiye "
        "aur koi rule nahi hona chahiye.",
        "Police ko full powers milni chahiye kyunki sirf strict action se hi problem solve hogi "
        "aur kuch nahi chahiye.",
    ],
    "balanced": [
        "Mujhe lagta hai tourism se local logon ko rozgaar milta hai. Lekin environment par bhi "
        "asar padta hai, so carrying capacity rules zaroori hain.",
        "Community policing se trust banta hai aur intelligence milti hai. Lekin rights ka "
        "dhyan rakhna zaroori hai, isliye oversight bhi chahiye.",
    ],
    "planted": [
        "Testland mein tourism share of GDP 30 percent hai in 2023, isliye tourism hi growth ka "
        "main engine hai and jobs bhi wahi deta hai.",
    ],
    "concession": [
        "Haan, shayad mera figure galat tha. Main data check karunga, lekin mera point abhi bhi "
        "valid hai.",
        "Sorry, I was wrong about that number.",
    ],
    "silence": [""],
}

SCRIPTS: dict[str, list[str]] = {
    # The orchestrator's observed session shape: background, planted error, balanced,
    # concession, then an honest unknown in a fresh topic.
    "observed": [
        "Kanpur mein repair shops bahut hain, lekin skilled training kam hai. ITI ke saath "
        "tie-up karke skilling badhaunga.",
        ANSWERS["planted"][0],
        ANSWERS["balanced"][0],
        ANSWERS["concession"][0],
        ANSWERS["thin"][0],
        ANSWERS["balanced"][1],
        ANSWERS["honest_unknown"][0],
    ],
    "honest_streak": [
        ANSWERS["balanced"][0],
        ANSWERS["honest_unknown"][0],
        ANSWERS["honest_unknown"][1],
        ANSWERS["honest_unknown"][2],
        ANSWERS["honest_unknown"][0],
        ANSWERS["honest_unknown"][1],
    ],
    "thin_streak": [ANSWERS["balanced"][0]] + [ANSWERS["thin"][i % 3] for i in range(8)],
    "silence_then_thin": [
        ANSWERS["balanced"][0],
        "",
        ANSWERS["thin"][0],
        ANSWERS["thin"][1],
        ANSWERS["thin"][2],
    ],
    "balanced_only": [ANSWERS["balanced"][i % 2] for i in range(9)],
}


@dataclass
class Played:
    analyses: list[Analysis]
    moves: list[BoardMove]
    lines: list[BoardLine]  # board lines for candidate turns (opener excluded)
    topics: list[str]


async def play(deps, form, tmp_path, answers: list[str], sid: str) -> Played:
    analyzer = TurnAnalyzer(deps)
    session = InterviewSession(
        deps=deps,
        form=form,
        recorder=TraceRecorder(tmp_path, sid, enabled=False),
        analyzer=analyzer,
        session_id=sid,
    )
    await session.open()
    topics: list[str] = []
    for text in answers:
        if session.state.finished:
            break
        topics.append(session.state.topic)
        await session.answer(text)
    await analyzer.close_session(sid)
    st = session.state
    return Played(
        analyses=[t.analysis for t in st.turns],
        moves=list(st.moves),
        lines=list(st.board_lines[1:]),
        topics=topics,
    )


def random_answers(seed: int, n: int = 9) -> list[str]:
    rng = random.Random(seed)
    kinds = list(ANSWERS)
    weights = [3, 3, 2, 3, 1, 1, 1]  # honest_unknown and thin are common on purpose
    return [rng.choice(ANSWERS[k]) for k in rng.choices(kinds, weights=weights, k=n)]


# ---------------------------------------------------------------------------
# Invariants (each returns a list of human-readable violations)
# ---------------------------------------------------------------------------


def honesty_never_punished(p: Played) -> list[str]:
    bad = []
    seen_in_topic: dict[str, int] = {}
    for i, (a, m) in enumerate(zip(p.analyses, p.moves, strict=True)):
        if not a.honest_unknown:
            continue
        nth = seen_in_topic.get(p.topics[i], 0)
        seen_in_topic[p.topics[i]] = nth + 1
        if m.move not in (Move.reassure, Move.move_on):
            bad.append(f"turn {i}: honest unknown got {m.move.value}")
        if nth == 0 and m.move != Move.reassure and m.rule_id != "CAP":
            bad.append(f"turn {i}: first honest unknown in topic got {m.move.value}")
    return bad


def no_pressure_after_reassure(p: Played) -> list[str]:
    return [
        f"turn {i}: pressure right after reassure"
        for i in range(1, len(p.moves))
        if p.moves[i - 1].move == Move.reassure and p.moves[i].move == Move.pressure
    ]


def pressure_capped(p: Played, per_topic: int = 2) -> list[str]:
    bad = []
    counts: dict[str, int] = {}
    for i, m in enumerate(p.moves):
        if m.move != Move.pressure:
            continue
        if i and p.moves[i - 1].move == Move.pressure:
            bad.append(f"turn {i}: pressure twice in a row")
        counts[p.topics[i]] = counts.get(p.topics[i], 0) + 1
        if counts[p.topics[i]] > per_topic:
            bad.append(f"turn {i}: pressure #{counts[p.topics[i]]} in topic {p.topics[i]}")
    return bad


def concession_acknowledged(p: Played) -> list[str]:
    bad = []
    for i, (a, m, line) in enumerate(zip(p.analyses, p.moves, p.lines, strict=True)):
        if a.concedes_error and m.move != Move.clarify:
            if m.preface != "acknowledge":
                bad.append(f"turn {i}: concession without acknowledge preface")
            if not line.text.startswith(ACKNOWLEDGE_LINES["hi-en"]):
                bad.append(f"turn {i}: line does not start with the acknowledgement")
            if m.move == Move.pressure:
                bad.append(f"turn {i}: concession was pressed")
    return bad


def balanced_not_pushed_back(p: Played) -> tuple[list[str], int, int]:
    """Strong balanced answers: never pushed back on. Returns (violations, pushed, total)
    over all balanced answers so the caller can bound the overall rate."""
    bad, pushed, total = [], 0, 0
    for i, (a, m) in enumerate(zip(p.analyses, p.moves, strict=True)):
        if a.balance != Balance.balanced:
            continue
        total += 1
        if m.move in PUSH_BACK:
            pushed += 1
            if a.is_strong:
                bad.append(f"turn {i}: strong balanced answer got {m.move.value}")
    return bad, pushed, total


def all_violations(p: Played) -> list[str]:
    return (
        honesty_never_punished(p)
        + no_pressure_after_reassure(p)
        + pressure_capped(p)
        + concession_acknowledged(p)
        + balanced_not_pushed_back(p)[0]
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCRIPTS))
async def test_scripted_sessions_keep_every_invariant(deps, form, tmp_path, name):
    played = await play(deps, form, tmp_path, SCRIPTS[name], f"inv-{name}")
    assert played.moves, "session produced no moves"
    assert all_violations(played) == []


async def test_observed_session_reassures_honest_unknown_and_acknowledges_concession(
    deps, form, tmp_path
):
    """The two moments the orchestrator flagged in the Phase-1 transcript."""
    p = await play(deps, form, tmp_path, SCRIPTS["observed"], "inv-observed-2")
    honest = [m for a, m in zip(p.analyses, p.moves, strict=True) if a.honest_unknown]
    assert honest and honest[0].move == Move.reassure
    conceded = [m for a, m in zip(p.analyses, p.moves, strict=True) if a.concedes_error]
    assert conceded and conceded[0].preface == "acknowledge"


async def test_honest_streak_reassures_once_then_moves_on(deps, form, tmp_path):
    p = await play(deps, form, tmp_path, SCRIPTS["honest_streak"], "inv-honest")
    by_topic: dict[str, list[Move]] = {}
    for a, m, topic in zip(p.analyses, p.moves, p.topics, strict=True):
        if a.honest_unknown:
            by_topic.setdefault(topic, []).append(m.move)
    for moves in by_topic.values():
        assert moves[0] == Move.reassure
        assert all(m == Move.move_on for m in moves[1:])


async def test_thin_streak_never_whiplashes(deps, form, tmp_path):
    p = await play(deps, form, tmp_path, SCRIPTS["thin_streak"], "inv-thin")
    assert Move.pressure in [m.move for m in p.moves]  # pressure still exists ...
    assert pressure_capped(p) == []  # ... but is capped


@pytest.mark.parametrize("seed", range(30))
async def test_random_sessions_keep_every_invariant(deps, form, tmp_path, seed):
    played = await play(deps, form, tmp_path, random_answers(seed), f"inv-rand-{seed}")
    assert all_violations(played) == [], random_answers(seed)


async def test_balanced_answers_are_rarely_pushed_back_on(deps, form, tmp_path):
    pushed = total = 0
    for seed in range(30):
        p = await play(deps, form, tmp_path, random_answers(seed), f"inv-bal-{seed}")
        _, k, n = balanced_not_pushed_back(p)
        pushed, total = pushed + k, total + n
    assert total >= 10
    assert pushed / total <= 0.1, f"{pushed}/{total} balanced answers were pushed back on"
