"""One news fetch when a real sitting opens, before the agenda plan.

The fake path never calls the network. A headline is kept only when it can sit in the
same briefing as the file in `data/affairs`: one sentence, no digit, an https link.
If the fetch fails or every title is dropped, the caller keeps that file and says so.
The lines are background for the plan and for every member. They are not the interview.
"""

from __future__ import annotations

import asyncio
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

from mockboard.brain.affairs import AffairsNote, affairs_line
from mockboard.brain.llm import is_offline_llm

# Public Google News RSS. No key. Titles are untrusted text.
NEWS_URL = (
    "https://news.google.com/rss/search?q=India+governance+OR+economy"
    "&hl=en-IN&gl=IN&ceid=IN:en"
)
TIMEOUT_S = 0.8
_MAX_NOTES = 3
_MAX_WORDS = 40
_DIGIT = re.compile(r"\d")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
_STOP = frozenset({
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "with", "from", "by",
    "at", "as", "is", "are", "was", "were", "has", "have", "had", "its", "their", "this",
    "that", "which", "who", "what", "when", "where", "how", "will", "would", "about",
    "after", "before", "into", "over", "under", "than", "then", "also", "not", "no",
    "yes", "been", "being", "says", "said", "say",
})


def headlines_from_rss(xml: str, *, as_of: str, limit: int = _MAX_NOTES) -> tuple[AffairsNote, ...]:
    """Turn an RSS document into briefing notes. A bad title is skipped, not repaired."""
    root = ET.fromstring(xml)
    notes: list[AffairsNote] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = (item.findtext("link") or "").strip()
        note = _note(title, link, as_of=as_of, index=len(notes))
        if note is None or note.line in seen:
            continue
        seen.add(note.line)
        notes.append(note)
        if len(notes) >= limit:
            break
    return tuple(notes)


def fetch_headlines(
    *, timeout_s: float = TIMEOUT_S, url: str = NEWS_URL, as_of: str | None = None
) -> tuple[AffairsNote, ...]:
    """Fetch once. Any failure, including a timeout, is an empty tuple."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MockBoard/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(200_000)
        text = raw.decode("utf-8", errors="replace")
        return headlines_from_rss(text, as_of=as_of or date.today().isoformat())
    except Exception:
        return ()


async def prepare_briefing(llm: object) -> tuple[tuple[AffairsNote, ...], str, str]:
    """`(notes, line, source)` for the opening plan.

    An offline model, and a Bedrock client with a test stub, return empty notes and
    source `""`: nothing is fetched, and the file briefing stands. A live botocore
    client fetches once. `search` means the notes replace the file for this sitting.
    `file` means the fetch gave nothing usable.
    """
    if not _live_client(llm):
        return (), "", ""
    notes = await asyncio.to_thread(fetch_headlines)
    if not notes:
        return (), "", "file"
    return notes, affairs_line(notes), "search"


def _live_client(llm: object) -> bool:
    """True only for the real Bedrock client. A stub passed as `client=` must not fetch."""
    if is_offline_llm(llm):
        return False
    clients = getattr(llm, "_clients", None)
    if not isinstance(clients, dict) or not clients:
        return False
    sample = next(iter(clients.values()))
    return type(sample).__module__.startswith("botocore")


def _note(title: str, link: str, *, as_of: str, index: int) -> AffairsNote | None:
    text = " ".join(html.unescape(title).split())
    source = "Google News"
    if " - " in text:
        head, tail = text.rsplit(" - ", 1)
        if head and tail and not _DIGIT.search(tail):
            text, source = head.strip(), " ".join(tail.split())
    if not text or _DIGIT.search(text) or "<" in text or ">" in text:
        return None
    if text.count(".") + text.count("?") + text.count("!") > 1:
        return None
    if text[-1] not in ".?!":
        text = text + "."
    if len(text.split()) > _MAX_WORDS:
        return None
    if not link.startswith("https://") or " " in link:
        return None
    words = _words(text)
    if not words:
        return None
    return AffairsNote(
        id=f"news-{index}",
        as_of=as_of,
        field="news",
        words=words,
        line=text,
        source=source,
        source_url=link,
    )


def _words(title: str) -> tuple[str, ...]:
    found: list[str] = []
    for token in _WORD.findall(title):
        if token.lower() in _STOP:
            continue
        if token not in found:
            found.append(token)
        if len(found) == 4:
            break
    return tuple(found)
