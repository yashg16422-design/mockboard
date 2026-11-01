"""A small session stand-in for director tests, and the P3 scripted trace.

Plays the part P4's `PanelHooks` will play: builds the context, runs the real strategist, applies
`decision.counted` and `decision.free`, and queues the finished turn. No model, no speech.

Print the trace from the repository root, inside `.venv`:
    PYTHONPATH=src:. python tests/director_sim.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from mockboard.brain.agenda import build_fallback_agenda, capped_budgets
from mockboard.brain.director import PanelDirector, compile_director_graph
from mockboard.brain.personas import PERSONA_DIR, load_personas
from mockboard.brain.requests import word_count
from mockboard.brain.schemas import (
    PANEL_FREE_TURNS,
    Analysis,
    Balance,
    CandidateForm,
    CompletedTurn,
    DirectorTurn,
    Move,
    PanelDecision,
    PanelSettings,
    StrategyContext,
    VoiceSignals,
)
from mockboard.brain.strategist import StrategyConfig, decide_move
from mockboard.modes.upsc import upsc_cap_move

FORM = CandidateForm(
    home_state="Uttar Pradesh",
    district="Kanpur",
    degree_subject="Electronics",
    hobbies=["cricket"],
)
ANSWER = "Mera maanna hai ki iske do pehlu hain aur dono par dhyan dena zaroori hai sir."


@dataclass
class Sim:
    cap: int | None = None
    free_turns: int = PANEL_FREE_TURNS
    wrap_up_due: bool = False
    session_id: str = "sim"
    graph: Any = None
    agenda: Any = None
    max_turns: int | None = None
    strict: bool = True  # an unexpected `director_error` must fail, not pass as a quiet fallback
    # What only `hooks.apply` may move.
    topic_index: int = 0
    turn_index: int = 0
    turns_in_topic: int = 0
    answers_used: int = 0
    free_used: int = 0
    last_move: Move | None = None
    strong_streak: int = 0
    honest_unknowns: int = 0
    finished: bool = False
    log: list[tuple[str, PanelDecision]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.agenda is None:
            self.agenda = build_fallback_agenda(FORM, capped_budgets(self.cap))
        names = {p.id: p.name for p in load_personas(PERSONA_DIR / "panel").values()}
        self.settings = PanelSettings(
            chairman="chairman", names=names, max_answers=self.cap, free_turns=self.free_turns
        )
        self.strategy = StrategyConfig(
            chairman="chairman",
            topic_members={s.id: s.member for s in self.agenda.segments},
        )
        self.director = PanelDirector(
            self.session_id,
            self.agenda,
            self.settings,
            graph=self.graph if self.graph is not None else compile_director_graph(),
        )
        if self.max_turns is None:
            self.max_turns = self.agenda.total_budget + self.free_turns

    @property
    def topic(self) -> str:
        return self.agenda.topics[self.topic_index]

    def build(
        self, text: str, *, latency: float | None = None, **fields: Any
    ) -> tuple[DirectorTurn, Analysis]:
        turn_id = f"t{self.turn_index + 1}"
        topics = self.agenda.topics
        ctx = StrategyContext(
            turn_id=turn_id,
            turn_index=self.turn_index,
            topic=self.topic,
            next_topic=topics[self.topic_index + 1] if self.topic_index + 1 < len(topics) else None,
            turns_in_topic=self.turns_in_topic,
            max_turns_in_topic=self.agenda.segment(self.topic).budget,
            last_move=self.last_move,
            strong_streak=self.strong_streak,
            honest_unknowns_in_topic=self.honest_unknowns,
            voice_signals=VoiceSignals(response_latency_s=latency),
        )
        values: dict[str, Any] = {
            "turn_id": turn_id,
            "word_count": word_count(text),
            "reasons": ["first reason", "second reason"] if text else [],
            "balance": Balance.unclear,
            "stance": "a two-sided view" if text else "",
        }
        analysis = Analysis(**{**values, **fields})
        base = decide_move(ctx, [analysis], [], self.strategy)
        if self.turn_index + 1 >= self.max_turns:
            base = upsc_cap_move(ctx, base, self.max_turns, self.strategy)
        turn = DirectorTurn(
            ctx=ctx,
            base_move=base,
            analysis=analysis,
            raw_text=text,
            answers_used=self.answers_used,
            free_used=self.free_used,
            wrap_up_due=self.wrap_up_due,
        )
        return turn, analysis

    def apply(self, decision: PanelDecision, analysis: Analysis, text: str) -> None:
        """What `PanelHooks.apply` will do (CTO_NOTES_12 §9.5)."""
        self.log.append((text, decision))
        self.turn_index += 1
        self.answers_used += decision.counted
        self.free_used += decision.free
        self.turns_in_topic += decision.counted
        self.last_move = decision.move.move
        self.finished = decision.finished
        self.strong_streak = self.strong_streak + 1 if analysis.is_strong else 0
        self.honest_unknowns += analysis.honest_unknown
        if decision.move.move == Move.move_on and not decision.finished:
            self.topic_index += 1
            self.turns_in_topic = 0
            self.honest_unknowns = 0
        spoken = None
        if not decision.line_complete and decision.move.move != Move.clarify:
            spoken = decision.seed or f"[{decision.move.move.value} line {self.turn_index}]"
        self.director.queue_completed(
            CompletedTurn(analysis=analysis, decision=decision, asked=spoken)
        )

    async def say(self, text: str = ANSWER, **kw: Any) -> PanelDecision:
        turn, analysis = self.build(text, **kw)
        decision = await self.director.decide_turn(turn)
        if self.strict and decision.director_error:
            raise AssertionError(decision.director_error)
        self.apply(decision, analysis, text)
        return decision

    async def plain_until_done(self, limit: int = 40) -> list[PanelDecision]:
        out = []
        while not self.finished and len(out) < limit:
            out.append(await self.say())
        return out


def _clip(text: str, width: int = 74) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def row(sim: Sim, index: int) -> str:
    text, d = sim.log[index]
    said = f'"{_clip(text, 62)}"' if text else "(silence)"
    cost = "counted" if d.counted else "free" if d.free else "neither"
    lines = [
        f"{d.turn_id:>4} {d.answered_segment:<15} {said}",
        f"     -> {d.node} | {d.move.move.value} by {d.move.member} | {d.move.rule_id} | {cost}"
        + (f" | request={d.request_kind}" if d.request_kind else "")
        + (f" | close={d.close_reason}" if d.close_reason else ""),
    ]
    lines += [f"        {p.member}: {_clip(p.text)}" for p in d.preface]
    if d.repeat_text:
        lines.append(f"        {d.move.member} verbatim: {_clip(d.repeat_text)}")
    if d.seed:
        lines.append(f"        {d.panel.member} seed[{d.seed_index}]: {_clip(d.seed)}")
    return "\n".join(lines)


def show(title: str, sim: Sim, start: int = 0) -> None:
    print(f"\n=== {title}")
    for index in range(start, len(sim.log)):
        print(row(sim, index))
    print(f"     answers_used={sim.answers_used} free_used={sim.free_used} "
          f"finished={sim.finished}")  # fmt: skip


async def main() -> None:
    normal = Sim()
    print("Opener (not a turn):", normal.director.memory.last_question)
    for _ in range(4):
        await normal.say()
    show("Normal run, uncapped: first four answers", normal)

    change = Sim()
    await change.say()
    await change.say()
    await change.say("tourism ke alawa kisi aur topic pe baat karte hain")
    await change.say()
    show("Topic change", change)
    print("     avoid terms in memory after the next turn:",
          change.director.memory.avoid_terms)  # fmt: skip

    repeat = Sim()
    await repeat.say()
    await repeat.say()
    await repeat.say("please repeat")
    await repeat.say("please repeat")
    await repeat.say()
    show("Repeat, twice, just after a hand-off", repeat)

    skip = Sim()
    await skip.say()
    await skip.say()
    await skip.say("next question please")
    await skip.say()
    show("Skip", skip)

    end = Sim()
    await end.say()
    await end.say("interview yahin khatam karte hain")
    await end.say("nahi nahi, chaliye aage badhte hain")
    await end.say()
    await end.say("I want to end the interview")
    await end.say("haan ji")
    show("End declined, then end confirmed", end)

    capped = Sim(cap=10)
    await capped.plain_until_done()
    show("Capped at 10, plain answers to the close", capped)


if __name__ == "__main__":
    asyncio.run(main())
