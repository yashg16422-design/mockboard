"""Ruling 36 (Defect I, owner's first real voice session): synthetic test cards never enter
any prompt outside the offline fake LLM. In the real run the board asked "Exampleland mein
tourism ka GDP share kya hai?", a quiz question built from a TEST CARD.

With no real cards, a UPSC session runs without fact checks (no librarian call, no fact
challenge) and the report says so."""

from __future__ import annotations

from conftest import answer
from mockboard.brain.cards import CardSet, card_set_version, load_cards, without_synthetic
from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import fake_responders
from mockboard.brain.graphs import NO_CARDS_REPORT_NOTE, TurnAnalyzer
from mockboard.brain.llm import FakeLLMClient, is_offline_llm
from mockboard.brain.schemas import FactCard, Move, Verdict
from mockboard.config import REPO_ROOT
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

TEST_DATA_WORDS = ("exampleland", "testland", "test card", "synthetic")
ANSWERS = (REPO_ROOT / "examples" / "fake_session.txt").read_text().splitlines()[7:]


class RealModeLLM(FakeLLMClient):
    """The fake's rule-based answers, but presenting as a real (non-offline) client, so the
    session takes the real-run path that Bedrock would."""

    offline = False


def data_cards(settings) -> CardSet:
    return load_cards(settings.cards_dir)  # all SYNTHETIC test cards today


def real_card() -> FactCard:
    # A structurally real (sourced) card, for this test only: the URL is a placeholder.
    return FactCard(id="test_real_shape", topic="tourism", statement="A sourced statement.",
                    value=5, unit="percent", year=2024, source_title="Placeholder source",
                    source_url="https://example.org/placeholder")


def test_offline_marker_follows_the_fake_and_its_wrappers(cards):
    class Wrapper:  # like the voice tests' SlowActor: delegates attribute lookups
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

    fake = FakeLLMClient()
    assert is_offline_llm(fake) and is_offline_llm(Wrapper(fake))
    assert not is_offline_llm(RealModeLLM()) and not is_offline_llm(object())


def test_real_run_has_no_test_card_in_any_prompt_prefix(settings):
    cards = data_cards(settings)
    assert cards.cards and cards.all_synthetic
    deps = build_deps(settings, RealModeLLM(responders=fake_responders(cards)), cards=cards)
    assert deps.cards.cards == {}
    for mode in ("upsc_interview", "debate"):
        prefix = deps.mode(mode).kit.shared_prefix.lower()
        assert not any(w in prefix for w in ("exampleland", "test card")), mode


def test_fake_runs_and_explicit_opt_in_keep_the_test_cards(settings, fake_llm):
    cards = data_cards(settings)
    assert build_deps(settings, fake_llm, cards=cards).cards is cards
    kept = build_deps(settings, RealModeLLM(), cards=cards, allow_synthetic_cards=True)
    assert kept.cards is cards


def test_real_cards_survive_and_only_test_cards_are_removed(settings):
    mixed = {**data_cards(settings).cards, "test_real_shape": real_card()}
    card_set = CardSet(cards=mixed, version=card_set_version(mixed.values()))
    out = without_synthetic(card_set)
    assert list(out.cards) == ["test_real_shape"] and out.version != card_set.version


async def test_real_run_without_real_cards_fact_checks_nothing_and_says_so(settings, form,
                                                                         tmp_path):
    cards = data_cards(settings)
    llm = RealModeLLM(responders=fake_responders(cards))
    deps = build_deps(settings, llm, cards=cards)
    s = InterviewSession(deps=deps, form=form, recorder=TraceRecorder(tmp_path, "r36"),
                         analyzer=TurnAnalyzer(deps), session_id="r36")
    await s.open()
    for text in ANSWERS:
        if s.state.finished or not text.strip():
            break
        await answer(s, text)
    report = await s.finish("en")
    assert llm.calls_named("librarian") == []
    assert Move.fact_challenge not in [m.move for m in s.state.moves]
    planted = [c for c in s.state.claim_ledger if c.value == 30]
    assert planted and all(c.verdict == Verdict.unverifiable for c in planted)
    assert NO_CARDS_REPORT_NOTE in report.notes
    statements = [c.statement.lower() for c in cards.cards.values()]
    for call in llm.calls:  # opener, listener, actor, judge: no card text anywhere
        system = call.system.lower()
        assert not any(w in system for w in TEST_DATA_WORDS[:3]), call.name
        text = (call.system + "\n" + call.user).lower()
        assert not any(st in text for st in statements), call.name
        assert "test card" not in text, call.name  # the candidate may say "Exampleland"


async def test_fake_run_report_has_no_such_note(deps, form, tmp_path):
    s = InterviewSession(deps=deps, form=form, recorder=TraceRecorder(tmp_path, "f36"),
                         analyzer=TurnAnalyzer(deps), session_id="f36")
    await s.open()
    await answer(s, ANSWERS[0])
    report = await s.finish("en")
    assert NO_CARDS_REPORT_NOTE not in report.notes
