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
    _cache: dict[str, Prompt] = field(default_factory=dict, compare=False, repr=False)

    def prompt(self, prompt_id: str) -> Prompt:
        if prompt_id not in self._cache:
            self._cache[prompt_id] = load_prompt(prompt_id, self.versions[prompt_id])
        return self._cache[prompt_id]

    def system(self, prompt_id: str, **values: str) -> list[PromptBlock]:
        """Shared prefix (cache checkpoint after it) + the role's rendered instructions."""
        return [
            PromptBlock(self.shared_prefix, cache=True),
            PromptBlock(self.prompt(prompt_id).render(**values)),
        ]

    @property
    def prefix_tokens_approx(self) -> int:
        return approx_tokens(self.shared_prefix)


def build_shared_prefix(personas: dict[str, Persona], card_set: CardSet, version: int = 1) -> str:
    """Stable ordering matters: any byte change here invalidates Nova's prompt cache."""
    shared = load_prompt("shared", version)
    return "\n\n".join([shared.body, render_persona_pack(personas), render_cards_block(card_set)])


def build_prompt_kit(
    personas: dict[str, Persona], card_set: CardSet, versions: dict[str, int]
) -> PromptKit:
    return PromptKit(
        shared_prefix=build_shared_prefix(personas, card_set),
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


def tag(name: str, content: str) -> str:
    """Wrap untrusted content in a named tag, neutralising any closing tag inside it."""
    safe = content.replace(f"</{name}>", f"</ {name}>")
    return f"<{name}>\n{safe}\n</{name}>"
