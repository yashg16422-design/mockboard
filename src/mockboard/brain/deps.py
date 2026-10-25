"""`BrainDeps`: everything the brain needs, built once per process from Settings.

It is passed to LangGraph as the run *context* (`graph.ainvoke(..., context=deps)`), so nodes
receive it through `runtime.context` instead of global variables. That keeps nodes testable:
a test builds `BrainDeps` with a `FakeLLMClient`.
"""

from __future__ import annotations

from dataclasses import dataclass

from mockboard.brain.cards import CardSet, load_cards
from mockboard.brain.llm import LLMClient
from mockboard.brain.numbers import Tolerance
from mockboard.brain.personas import chairman, load_personas
from mockboard.brain.prompts import PromptKit, build_prompt_kit, check_prefix_budget
from mockboard.brain.schemas import Persona
from mockboard.brain.strategist import StrategyConfig
from mockboard.config import Settings

BACKGROUND_TOPIC = "background"  # the form-driven opener's pseudo-topic (one turn)


@dataclass(frozen=True)
class BrainDeps:
    llm: LLMClient
    kit: PromptKit
    cards: CardSet
    personas: dict[str, Persona]
    tolerance: Tolerance
    strategy: StrategyConfig
    settings: Settings

    @property
    def retry_max_attempts(self) -> int:
        return self.settings.graph_retry_max_attempts


def strategy_config(
    settings: Settings, personas: dict[str, Persona], cards: CardSet
) -> StrategyConfig:
    topic_members = {
        topic: p.id for p in personas.values() if p.role != "chairman" for topic in p.topics
    }
    return StrategyConfig(
        chairman=chairman(personas).id,
        topic_members=topic_members,
        card_topics={c.id: c.topic for c in cards.cards.values()},
        fact_challenge_min_delay_turns=settings.fact_challenge_min_delay_turns,
        strong_streak_to_move_on=settings.strong_streak_to_move_on,
        reassure_latency_s=settings.reassure_latency_s,
        board_interrupt_enabled=settings.feature_board_interrupt,
        interrupt_after_words=settings.interrupt_after_words,
    )


def build_deps(settings: Settings, llm: LLMClient, cards: CardSet | None = None) -> BrainDeps:
    """Load personas and cards, build the cached prompt prefix and check its size budget.

    `cards` overrides loading from `settings.cards_dir` (tests pass the built-in set).
    """
    personas = load_personas()
    if cards is None:
        cards = load_cards(settings.cards_dir, settings.topics)
    kit = build_prompt_kit(personas, cards, settings.prompt_versions())
    check_prefix_budget(kit.shared_prefix, settings.cache_prefix_token_budget)
    return BrainDeps(
        llm=llm,
        kit=kit,
        cards=cards,
        personas=personas,
        tolerance=Tolerance(
            rel=settings.numeric_rel_tolerance, abs_pp=settings.numeric_abs_tolerance_pp
        ),
        strategy=strategy_config(settings, personas, cards),
        settings=settings,
    )
