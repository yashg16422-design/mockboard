"""`BrainDeps`: everything the brain needs, built once per process from Settings.

It is passed to LangGraph as the run *context* (`graph.ainvoke(..., context=deps)`), so nodes
receive it through `runtime.context` instead of global variables. That keeps nodes testable:
a test builds `BrainDeps` with a `FakeLLMClient`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from mockboard.brain.cards import CardSet, load_cards
from mockboard.brain.librarian import VoteCache
from mockboard.brain.llm import LLMClient
from mockboard.brain.numbers import Tolerance
from mockboard.brain.personas import chairman, load_personas
from mockboard.brain.prompts import PromptKit, build_prompt_kit, check_prefix_budget
from mockboard.brain.schemas import Persona
from mockboard.brain.strategist import StrategyConfig
from mockboard.config import Settings
from mockboard.modes import DEFAULT_MODE, ModeProfile, get_profile, mode_ids

BACKGROUND_TOPIC = "background"  # the form-driven opener's pseudo-topic (one turn)


@dataclass(frozen=True)
class ModeRuntime:
    """One mode's built pieces (ADR-009): its profile, its own prompt kit (so one cached
    prefix per mode), its persona pack and its strategy wiring. Built once per process."""

    profile: ModeProfile
    kit: PromptKit
    personas: dict[str, Persona]
    strategy: StrategyConfig


@dataclass(frozen=True)
class BrainDeps:
    llm: LLMClient
    kit: PromptKit
    cards: CardSet
    personas: dict[str, Persona]
    tolerance: Tolerance
    strategy: StrategyConfig
    settings: Settings
    # Process-wide cache of real librarian votes (ruling 10). One per BrainDeps, and
    # BrainDeps is built once per process, so every session shares it. It never holds a
    # fallback vote, which is what makes sharing it across sessions safe.
    vote_cache: VoteCache = field(default_factory=VoteCache)
    # Every registered mode's runtime, by mode id. `kit`, `personas` and `strategy` above
    # are the default mode's (upsc_interview), so every existing caller is unchanged.
    modes: Mapping[str, ModeRuntime] = field(default_factory=dict)

    def mode(self, mode_id: str | None) -> ModeRuntime:
        key = mode_id or DEFAULT_MODE
        if key in self.modes:
            return self.modes[key]
        if key == DEFAULT_MODE:  # a BrainDeps built by hand (tests) has no mode table
            return ModeRuntime(get_profile(key), self.kit, self.personas, self.strategy)
        raise ValueError(f"mode {key!r} is not built in this BrainDeps")

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
    strategy = strategy_config(settings, personas, cards)
    modes = {DEFAULT_MODE: ModeRuntime(get_profile(DEFAULT_MODE), kit, personas, strategy)}
    for mode_id in mode_ids():
        if mode_id != DEFAULT_MODE:
            modes[mode_id] = build_mode_runtime(settings, get_profile(mode_id), cards)
    return BrainDeps(
        llm=llm,
        kit=kit,
        cards=cards,
        personas=personas,
        tolerance=Tolerance(
            rel=settings.numeric_rel_tolerance, abs_pp=settings.numeric_abs_tolerance_pp
        ),
        strategy=strategy,
        settings=settings,
        vote_cache=VoteCache(settings.librarian_vote_cache_size),
        modes=modes,
    )


def build_mode_runtime(settings: Settings, profile: ModeProfile, cards: CardSet) -> ModeRuntime:
    """A non-default mode's kit, personas and strategy wiring, from its profile."""
    personas = load_personas(profile.persona_dir)
    kit = build_prompt_kit(
        personas,
        cards,
        settings.prompt_versions(profile.id),
        shared_id=profile.prompt_id("shared"),
        persona_heading=profile.persona_heading,
    )
    check_prefix_budget(kit.shared_prefix, settings.cache_prefix_token_budget)
    return ModeRuntime(profile, kit, personas, strategy_config(settings, personas, cards))
