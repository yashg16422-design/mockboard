"""A short briefing every member has read before the sitting.

This is not a web search and not a fact card, and it is not the subject of the interview.
Each note is one sentence with no digit, kept next to the URL it came from. Only the
sentences reach the actor. The URL, the date and any name of a real person stay in the file.

The candidate still chooses the issue. A member may use one clause from one sentence only
after the candidate has already brought that subject up (actor.v8). Prisma hears a note's
words only after the candidate has said one of them. A note goes stale; replace the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from mockboard.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "data" / "affairs" / "notes.yaml"
_DIGIT = re.compile(r"\d")
_MAX_WORDS = 45
_MAX_NOTES = 6


@dataclass(frozen=True)
class AffairsNote:
    id: str
    as_of: str
    field: str
    words: tuple[str, ...]
    line: str
    source: str
    source_url: str


def load_affairs(path: Path = DEFAULT_PATH) -> tuple[AffairsNote, ...]:
    """Load the notes, or an empty tuple when the file is absent. A bad note raises."""
    if not path.is_file():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError("affairs file must be a list")
    if len(raw) > _MAX_NOTES:
        raise ValueError(f"at most {_MAX_NOTES} affairs notes")
    notes = tuple(_one(item) for item in raw)
    ids = [n.id for n in notes]
    if len(ids) != len(set(ids)):
        raise ValueError("affairs note ids must be unique")
    return notes


def affairs_line(notes: tuple[AffairsNote, ...] | None = None) -> str:
    """The sentences the actor may draw on. No URL, no date, no digit."""
    notes = load_affairs() if notes is None else notes
    return "\n".join(f"[{n.field}] {n.line}" if n.field else n.line for n in notes)


def affairs_words(notes: tuple[AffairsNote, ...] | None = None) -> tuple[str, ...]:
    """Every briefing word. Prisma does not receive these until the candidate says one."""
    notes = load_affairs() if notes is None else notes
    words: list[str] = []
    seen: set[str] = set()
    for note in notes:
        for word in note.words:
            key = word.lower()
            if key not in seen:
                seen.add(key)
                words.append(word)
    return tuple(words)


def words_heard(text: str, notes: tuple[AffairsNote, ...] | None = None) -> tuple[str, ...]:
    """Briefing words to favour in Prisma, only for a note the candidate has already named."""
    notes = load_affairs() if notes is None else notes
    hay = text or ""
    out: list[str] = []
    seen: set[str] = set()
    for note in notes:
        if any(_mentioned(hay, word) for word in note.words):
            for word in note.words:
                key = word.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(word)
    return tuple(out)


def _mentioned(hay: str, word: str) -> bool:
    return re.search(rf"(?i)(?<!\w){re.escape(word)}(?!\w)", hay) is not None


def _one(item: object) -> AffairsNote:
    if not isinstance(item, dict):
        raise ValueError("each affairs note must be a mapping")
    needed = ("id", "as_of", "field", "words", "line", "source", "source_url")
    missing = [k for k in needed if not item.get(k)]
    if missing:
        raise ValueError(f"affairs note missing {missing}")
    line = " ".join(str(item["line"]).split())
    if _DIGIT.search(line):
        raise ValueError("an affairs line must not contain a digit")
    if len(line.split()) > _MAX_WORDS:
        raise ValueError(f"an affairs line must be at most {_MAX_WORDS} words")
    if line.count(".") + line.count("?") + line.count("!") > 1:
        raise ValueError("an affairs line must be one sentence")
    words = tuple(" ".join(str(w).split()) for w in item["words"])
    if not words or any(_DIGIT.search(w) for w in words):
        raise ValueError("affairs words must be present and must not contain a digit")
    url = str(item["source_url"]).strip()
    if not url.startswith("https://"):
        raise ValueError("an affairs source_url must be https")
    as_of = str(item["as_of"]).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise ValueError("affairs as_of must be YYYY-MM-DD")
    field = str(item["field"]).strip()
    if not field or _DIGIT.search(field) or " " in field:
        raise ValueError("affairs field must be one word and must not contain a digit")
    return AffairsNote(
        id=str(item["id"]).strip(),
        as_of=as_of,
        field=field,
        words=words,
        line=line,
        source=" ".join(str(item["source"]).split()),
        source_url=url,
    )
