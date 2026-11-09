"""Load versioned prompt files (`src/mockboard/prompts/<id>.v<N>.md`) and build the shared,
cacheable system prefix.

Prompts are never inline strings in code: each has an id, a version and a small YAML header,
and every trace line records which versions were used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from mockboard.brain.cards import CardSet, render_cards_block
from mockboard.brain.llm import PromptBlock, approx_tokens
from mockboard.brain.personas import render_persona_pack
from mockboard.brain.schemas import Persona

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
_VAR = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    meta: dict[str, Any]
    body: str

    def variables(self) -> set[str]:
        return set(_VAR.findall(self.body))

    def render(self, **values: str) -> str:
        missing = self.variables() - set(values)
        if missing:
            raise KeyError(f"prompt {self.id}.v{self.version} missing variables: {sorted(missing)}")
        return _VAR.sub(lambda m: values[m.group(1)], self.body)


@lru_cache(maxsize=64)
def load_prompt(prompt_id: str, version: int, directory: Path = PROMPT_DIR) -> Prompt:
    path = directory / f"{prompt_id}.v{version}.md"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing YAML header")
    _, header, body = text.split("---", 2)
    meta = yaml.safe_load(header) or {}
    if meta.get("id") != prompt_id or int(meta.get("version", -1)) != version:
        raise ValueError(f"{path}: header id/version does not match file name")
    return Prompt(id=prompt_id, version=version, meta=meta, body=body.strip())


@dataclass(frozen=True)
class PromptKit:
    """Everything a node needs to build its system prompt."""

    shared_prefix: str
    versions: dict[str, int]
    card_set_version: str
    # Keyed by (id, version): `dataclasses.replace(kit, versions=...)` shares this dict, and a key
    # by id alone served the old version to the copy (found Nov 8, comparing agenda_plan v3/v4).
    _cache: dict[tuple[str, int], Prompt] = field(default_factory=dict, compare=False, repr=False)

    def prompt(self, prompt_id: str) -> Prompt:
        key = (prompt_id, self.versions[prompt_id])
        if key not in self._cache:
            self._cache[key] = load_prompt(*key)
        return self._cache[key]

    def system(self, prompt_id: str, **values: str) -> list[PromptBlock]:
        """Shared prefix (cache checkpoint after it) + the role's rendered instructions."""
        return [
            PromptBlock(self.shared_prefix, cache=True),
            PromptBlock(self.prompt(prompt_id).render(**values)),
        ]

    @property
    def prefix_tokens_approx(self) -> int:
        return approx_tokens(self.shared_prefix)


def build_shared_prefix(
    personas: dict[str, Persona],
    card_set: CardSet,
    version: int = 1,
    *,
    shared_id: str = "shared",
    persona_heading: str | None = None,
) -> str:
    """Stable ordering matters: any byte change here invalidates Nova's prompt cache.

    One prefix per mode (ADR-009): `shared_id` names the mode's shared prompt and
    `persona_heading` its persona pack's heading; the UPSC defaults are unchanged."""
    shared = load_prompt(shared_id, version)
    pack = (
        render_persona_pack(personas)
        if persona_heading is None
        else render_persona_pack(personas, persona_heading)
    )
    return "\n\n".join([shared.body, pack, render_cards_block(card_set)])


def build_prompt_kit(
    personas: dict[str, Persona],
    card_set: CardSet,
    versions: dict[str, int],
    *,
    shared_id: str = "shared",
    persona_heading: str | None = None,
) -> PromptKit:
    return PromptKit(
        shared_prefix=build_shared_prefix(
            personas,
            card_set,
            versions.get(shared_id, 1),
            shared_id=shared_id,
            persona_heading=persona_heading,
        ),
        versions=dict(versions),
        card_set_version=card_set.version,
    )


def check_prefix_budget(prefix: str, budget_tokens: int) -> int:
    """Raise if the cached prefix exceeds the budget. Uses the chars/4 APPROXIMATION."""
    tokens = approx_tokens(prefix)
    if tokens > budget_tokens:
        raise ValueError(
            f"cached system prefix is ~{tokens} tokens (approx, chars/4), over the "
            f"{budget_tokens}-token budget; Nova's prompt cache supports at most 20K tokens"
        )
    return tokens


# Anything that looks like an XML/HTML tag: "<x>", "</X >", "< /x>", "<x a='1'>", "<x/>".
_TAG_LIKE = re.compile(r"<\s*/?\s*[A-Za-z_][\w:.\-]*(?:\s[^<>]*)?\s*/?\s*>")


def _defang(match: re.Match[str]) -> str:
    return match.group(0).replace("<", "\u2039").replace(">", "\u203a")


def escape_tags(content: str, name: str) -> str:
    """Neutralise every tag-like token in untrusted content, whatever its case or spacing,
    by swapping its angle brackets for look-alike quotation marks (U+2039/U+203A).

    The model still reads the words, but nothing inside can close our delimiter or open a
    fake one (CTO_NOTES_02 should-fix 5). An unterminated closing tag for our own name
    ("</candidate_speech" with no ">") is neutralised too.
    """
    safe = _TAG_LIKE.sub(_defang, content)
    return re.sub(
        rf"<(\s*/\s*{re.escape(name)})", "\u2039\\1", safe, flags=re.IGNORECASE
    )


def tag(name: str, content: str) -> str:
    """Wrap untrusted content in a named tag; nothing inside can close or forge a tag."""
    return f"<{name}>\n{escape_tags(content, name)}\n</{name}>"
