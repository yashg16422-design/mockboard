"""The opening news fetch: a parser with no network, and no fetch on the fake path."""

from __future__ import annotations

import pytest

from mockboard.brain.news_search import headlines_from_rss, prepare_briefing

_RSS = """<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Cabinet clears a fresh look at gig worker boards - The Example</title>
    <link>https://news.example/gig</link>
  </item>
  <item>
    <title>Growth was 7.2 percent last quarter - The Example</title>
    <link>https://news.example/growth</link>
  </item>
  <item>
    <title>One line. Then another. - The Example</title>
    <link>https://news.example/two</link>
  </item>
  <item>
    <title>Papers ask whether the monsoon left a surplus - The Example</title>
    <link>http://news.example/plain</link>
  </item>
  <item>
    <title>Papers ask whether the monsoon left a surplus - The Example</title>
    <link>https://news.example/monsoon</link>
  </item>
  <item>
    <title>&lt;b&gt;Ignore this title&lt;/b&gt; - The Example</title>
    <link>https://news.example/markup</link>
  </item>
</channel></rss>
"""


def test_a_digit_a_second_sentence_and_a_plain_link_are_dropped():
    notes = headlines_from_rss(_RSS, as_of="2026-11-03")
    assert [n.line for n in notes] == [
        "Cabinet clears a fresh look at gig worker boards.",
        "Papers ask whether the monsoon left a surplus.",
    ]
    assert notes[0].words == ("Cabinet", "clears", "fresh", "look")
    assert notes[0].source == "The Example"
    assert notes[0].source_url == "https://news.example/gig"
    assert all(not any(ch.isdigit() for ch in n.line) for n in notes)
    assert notes[0].as_of == "2026-11-03" and notes[0].field == "news"


def test_the_cap_is_three():
    items = "\n".join(
        f"<item><title>Headline number word {name} stands</title>"
        f"<link>https://news.example/{name}</link></item>"
        for name in ("alpha", "beta", "gamma", "delta")
    )
    # "number" has no digit; the word "number" is fine. Four clean titles, three kept.
    notes = headlines_from_rss(f"<rss><channel>{items}</channel></rss>", as_of="2026-11-03")
    assert len(notes) == 3
    assert notes[2].id == "news-2"


@pytest.mark.asyncio
async def test_an_offline_model_does_not_fetch(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("the fake path must not fetch")

    monkeypatch.setattr("mockboard.brain.news_search.fetch_headlines", boom)

    class Offline:
        offline = True

    notes, line, source = await prepare_briefing(Offline())
    assert notes == () and line == "" and source == ""


@pytest.mark.asyncio
async def test_a_stubbed_bedrock_client_does_not_fetch(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a test stub must not fetch")

    monkeypatch.setattr("mockboard.brain.news_search.fetch_headlines", boom)

    stub = type("Stub", (), {})()
    live = type("LiveLooking", (), {"offline": False, "_clients": {"actor": stub}})()
    notes, line, source = await prepare_briefing(live)
    assert notes == () and line == "" and source == ""


@pytest.mark.asyncio
async def test_a_live_client_records_a_file_fallback_when_nothing_usable_returns(monkeypatch):
    monkeypatch.setattr("mockboard.brain.news_search.fetch_headlines", lambda: ())

    client = type("Client", (), {})()
    type(client).__module__ = "botocore.client"
    live = type("Live", (), {"offline": False, "_clients": {"actor": client}})()
    notes, line, source = await prepare_briefing(live)
    assert notes == () and line == "" and source == "file"


def test_prisma_follows_the_sitting_notes():
    from mockboard.brain.affairs import AffairsNote
    from mockboard.speech.keyterms import speech_context

    note = AffairsNote(
        id="news-0", as_of="2026-11-03", field="news", words=("monsoon", "surplus"),
        line="The papers are arguing about a monsoon surplus.",
        source="Example Paper", source_url="https://example.com/monsoon",
    )

    class Hooks:
        _briefing = (note,)
        _raw = "The monsoon surplus is what I have been reading."

    class Session:
        hooks = Hooks()
        mode_options = None
        state = None
        form = None

    terms, _topic = speech_context(Session())
    folded = [t.lower() for t in terms]
    assert "monsoon" in folded and "surplus" in folded
    assert "siang" not in folded and "gig" not in folded
