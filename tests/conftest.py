"""Shared fixtures. Everything runs offline: FakeLLMClient only, built-in synthetic cards."""

from __future__ import annotations

import os

import pytest

from mockboard.brain.cards import builtin_test_cards
from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.llm import FakeLLMClient
from mockboard.brain.numbers import Tolerance
from mockboard.brain.schemas import (
    Analysis,
    Balance,
    CandidateForm,
    Claim,
    ClaimKind,
    Segment,
    StrategyContext,
)
from mockboard.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the developer's real environment (flags, keys, regions) and never
    read the developer's `.env` file: with a real GNANI_API_KEY in `.env`, code under test
    that builds `Settings()` would otherwise find a key and start real services (seen on the
    owner's Mac: the voice-lab test started a real server and waited)."""
    for key in list(os.environ):
        if key.startswith(("FEATURE_", "GNANI_", "LANGFUSE_", "REPORT_", "CACHE_PREFIX")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        graph_retry_initial_interval_s=0.001,
        fast_graph_retry_initial_interval_s=0.001,
        graph_retry_jitter=False,  # LangGraph's jitter adds up to 1 s per retry
        trace_dir=tmp_path / "traces",
        report_dir=tmp_path / "reports",
    )


@pytest.fixture
def cards():
    return builtin_test_cards()


@pytest.fixture
def tolerance() -> Tolerance:
    return Tolerance()


@pytest.fixture
def fake_llm(cards) -> FakeLLMClient:
    return make_fake_llm(cards)


@pytest.fixture
def deps(settings, cards, fake_llm):
    return build_deps(settings, fake_llm, cards=cards)


@pytest.fixture
def form() -> CandidateForm:
    return CandidateForm(
        home_state="Uttar Pradesh",
        district="Kanpur Nagar",
        degree_subject="Electronics",
        hobbies=["chess", "cycling"],
        interview_language="hinglish",
    )


def make_claim(
    cid: str = "t0.s0.c0",
    *,
    kind: ClaimKind = ClaimKind.numeric,
    value: float | None = 30,
    unit: str | None = "percent",
    year: int | None = 2023,
    turn_index: int = 0,
    claim_en: str = "Testland tourism share of GDP is 30 percent in 2023",
    **kw,
) -> Claim:
    turn_id = cid.split(".")[0]
    text = kw.pop("text", claim_en)
    return Claim(
        id=cid,
        text=text,
        claim_en=claim_en,
        kind=kind,
        value=value,
        unit=unit,
        year=year,
        turn_id=turn_id,
        segment_id="s0",
        turn_index=turn_index,
        **kw,
    )


def make_analysis(
    turn_id: str = "t1",
    *,
    words: int = 40,
    reasons: int = 2,
    balance: Balance = Balance.balanced,
    **kw,
) -> Analysis:
    return Analysis(
        turn_id=turn_id,
        segment_id="s0",
        word_count=words,
        reasons=[f"reason {i}" for i in range(reasons)],
        balance=balance,
        **kw,
    )


def make_ctx(turn_index: int = 1, topic: str = "tourism", **kw) -> StrategyContext:
    return StrategyContext(
        turn_id=f"t{turn_index}",
        turn_index=turn_index,
        topic=topic,
        next_topic=kw.pop("next_topic", "internal_security"),
        **kw,
    )


def seg(text: str, sid: str = "s0", index: int = 0) -> Segment:
    return Segment(segment_id=sid, segment_index=index, text=text)


async def answer(session, text: str, *, deadline_s: float = 30.0, think_s: float = 5.0):
    """Play one typed answer through the session API the voice path uses: the answer is split
    into transcript segments (as Prisma's VAD would), each goes to `submit_segment`, then
    `end_of_turn` picks the move and `speak` phrases it.

    `think_s` stands in for the candidate's thinking time before answering (the CLI does the
    same for scripted runs): background fact checks may finish during it, never during the
    move's own path."""
    from mockboard.session import split_segments

    if think_s:
        await session.settle_fact_checks(think_s)
    for segment in split_segments(text, "-"):
        if segment.text:
            session.submit_segment(segment.text, segment_index=segment.segment_index)
    await session.end_of_turn(deadline_s)
    return await session.speak()
