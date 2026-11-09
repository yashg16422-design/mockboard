"""Ruling 73 items 1 and 2: a refusal covers the topic being discussed, not the whole form list, and
an alias like "Kanpur Nagar" bans the bare name while "Public Administration" does not ban the word
"administration" in unrelated lines. The matching itself is in tests/test_avoid_terms.py; these run
it in a whole fake panel session."""

from __future__ import annotations

from conftest import answer
from mockboard.brain.agenda import build_fallback_agenda
from mockboard.brain.schemas import CandidateForm
from test_panel_invariants import PLAIN, start, texts


def fallback_for(form):
    return build_fallback_agenda(form)


def texts_of(agenda, segment_id: str) -> list[str]:
    return [s.text for s in agenda.segment(segment_id).seeds]


async def talk_until(session, segment_index: int, position: int) -> None:
    n = 0
    while not session.state.finished and not (
        session.state.topic_index == segment_index and session.state.turns_in_topic == position
    ):
        await answer(session, PLAIN[n % len(PLAIN)], think_s=0)
        n += 1


async def test_leaving_chess_refuses_chess_and_not_the_other_hobbies(
    settings, cards, tmp_path
):
    form = CandidateForm(
        home_state="Uttar Pradesh", district="Kanpur Nagar", degree_subject="Electronics",
        hobbies=["chess", "cycling"], optional_subject="Public Administration",
    )
    session, _, _ = await start(settings, cards, form, tmp_path)
    await talk_until(session, 4, 0)  # Professor Sen's segment (last since Nov 9); chess first
    hooks = session.hooks
    await answer(session, "aage badhiye", think_s=0)
    refused = " ".join(hooks._refused)
    assert "chess" in refused.lower()
    assert "cycling" not in refused.lower() and "administration" not in refused.lower()
    await session.close()


async def test_a_refusal_that_names_nothing_refuses_the_segments_whole_list(
    settings, cards, tmp_path
):
    form = CandidateForm(
        home_state="Uttar Pradesh", district="Kanpur Nagar", degree_subject="Electronics",
        hobbies=["chess", "cycling"],
    )
    session, _, _ = await start(settings, cards, form, tmp_path)
    await talk_until(session, 3, 1)  # a follow-up in Menon's segment: a probe was last asked
    hooks = session.hooks
    await answer(session, "kisi aur topic pe baat karte hain", think_s=0)
    refused = " ".join(hooks._refused).lower()
    assert "kanpur" in refused and "uttar" in refused
    await session.close()


async def test_a_refused_district_never_returns_even_as_the_bare_name(
    settings, cards, form, tmp_path
):
    """Ruling 73 item 2: the form says "Kanpur Nagar", the board says "Kanpur"."""
    from mockboard.brain.avoid_terms import find_avoided

    session, _, _ = await start(settings, cards, form, tmp_path)
    await talk_until(session, 1, 0)  # Menon's first question names the district
    await answer(session, "kisi aur topic pe baat karte hain", think_s=0)
    after = []
    for i in range(40):
        if session.state.finished:
            break
        out = await answer(session, PLAIN[i % 3], think_s=0)
        after += texts(out)
    assert after and all(find_avoided(t, session.hooks._refused) is None for t in after)
    assert not any("Kanpur" in t for t in after)
    await session.close()


async def test_a_public_administration_refusal_leaves_the_word_administration_free(
    settings, cards, tmp_path
):
    form = CandidateForm(
        home_state="Kerala", degree_subject="Botany", optional_subject="Public Administration",
        hobbies=["chess"], interview_language="english",
    )
    session, _, _ = await start(settings, cards, form, tmp_path)
    from mockboard.brain.avoid_terms import expand_terms, find_avoided

    refused = expand_terms([], ["Public Administration"])
    assert find_avoided("Which public administration reform would you attempt?", refused)
    unrelated = "In what way would your subject help you in administration?"
    assert find_avoided(unrelated, refused) is None
    degree = texts_of(fallback_for(form), "degree")  # the template says "in administration"
    assert any("administration" in t.lower() for t in degree)
    assert all(find_avoided(t, refused) is None for t in degree)
    await session.close()


