"""One sourced current-affairs note the pressing member may offer.

This is not a web search and not a fact card. The note is a single sentence with no digit,
kept next to the URL it came from. Only the sentence reaches the actor. The URL, the date and
any name of a real person stay in the file, so the spoken line cannot pick up a number.

The seed still asks which issue the candidate has followed. If they name another issue, the
actor leaves this sentence (actor.v7). A note goes stale; replace the file, do not edit a
spoken line into the seed.
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
_MAX_NOTES = 3


@dataclass(frozen=True)
class AffairsNote:
    id: str
    as_of: str
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
    """The sentences the actor may say. No URL, no date, no digit."""
    notes = load_affairs() if notes is None else notes
    return "\n".join(n.line for n in notes)


def affairs_words(notes: tuple[AffairsNote, ...] | None = None) -> tuple[str, ...]:
    """Words Prisma v3.0 may favour while the current-affairs member is speaking."""
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


def _one(item: object) -> AffairsNote:
    if not isinstance(item, dict):
        raise ValueError("each affairs note must be a mapping")
    needed = ("id", "as_of", "words", "line", "source", "source_url")
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
    return AffairsNote(
        id=str(item["id"]).strip(),
        as_of=as_of,
        words=words,
        line=line,
        source=" ".join(str(item["source"]).split()),
        source_url=url,
    )
