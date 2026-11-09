"""The "client brain": an `LLMClient` that never calls a model (CTO_NOTES_18 §1).

Over MCP the person's own assistant is the model. It sends its analysis of each answer with
`next_turn` and speaks the board's lines in its own words; later it may send draft scores or study
notes. This class lets the real `InterviewSession` (director, agenda, request rules, guards,
reports) run unchanged on top of that:

* `listener`: the deterministic word-list analysis, with the client's fields laid over it.
  Claims are always empty: there are no fact cards, so nothing may be fact-challenged.
* `actor`, `opener`: the deterministic draft line (the same stand-in `--fake` uses). Our guards
  still check it, and it is handed to the client as a draft.
* `judge`, `partner_summary` and anything else: `LLMOutputError`, so the existing code-only
  report fallbacks run. The client's own draft scores or notes are checked later, directly, by
  the existing citation checks (`mcp/service.py`); no call name pretends to be a model.
* `repair`: no likely mishearings (there is no speech recogniser of ours here).

`offline = False` on purpose: synthetic test cards never enter (Ruling 36), and the news briefing
never fetches (`news_search._live_client`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mockboard.brain.cards import CardSet
from mockboard.brain.fake_responders import (
    listener_responder,
    make_actor_responder,
    opener_responder,
)
from mockboard.brain.llm import FakeLLMClient, LLMOutputError

MAX_FIELD_CHARS = 200
MAX_REASONS = 4
_BOOL_FIELDS = ("honest_unknown", "concedes_error", "garbled")
_BALANCES = ("balanced", "one_sided", "unclear")
_REQUESTS = ("none", "change_topic", "repeat", "clarify_question", "skip", "end")

EMPTY_CARDS = CardSet(cards={}, version="none (MCP: no fact cards)")


def _short(text: object) -> str:
    return " ".join(str(text).split())[:MAX_FIELD_CHARS] if isinstance(text, str) else ""


def clean_analysis(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The client's analysis, reduced to the listener fields we use, each typed and capped.
    Unknown keys are dropped; a wrong type is ignored rather than an error."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    if isinstance(raw.get("stance"), str):
        out["stance"] = _short(raw["stance"])
    reasons = raw.get("reasons")
    if isinstance(reasons, list):
        out["reasons"] = [_short(r) for r in reasons if isinstance(r, str) and r.strip()][
            :MAX_REASONS
        ]
    if raw.get("balance") in _BALANCES:
        out["balance"] = raw["balance"]
    for name in _BOOL_FIELDS:
        if isinstance(raw.get(name), bool):
            out[name] = raw[name]
    request = raw.get("candidate_request")
    if isinstance(request, str) and request.strip().lower() in _REQUESTS:
        out["candidate_request"] = request.strip().lower()
    return out


class ClientBrainLLM(FakeLLMClient):
    """See the module docstring. One per MCP session."""

    model_id = "client-model-via-mcp"
    offline = False  # Ruling 36: never the synthetic cards; and no news fetch

    def __init__(self) -> None:
        self.analysis: dict[str, Any] = {}  # set by `next_turn`, for this turn only
        super().__init__(responders=self._responders_table())

    def _responders_table(self) -> dict[str, Callable[[str, str], Any]]:
        return {
            "listener": self._listener,
            "actor": make_actor_responder(),
            "opener": opener_responder,
            "repair": lambda system, user: {"repairs": []},
        }

    def _listener(self, system: str, user: str) -> dict[str, Any]:
        out = listener_responder(system, user)
        out.update(self.analysis)
        out["claims"] = []  # no fact cards over MCP: nothing is ever fact-challenged
        if "candidate_request" not in system:  # only the panel listener has this field
            out.pop("candidate_request", None)
        return out

    def _next(self, name: str, system: str, user: str) -> Any:
        if name not in self._responders and not self._script.get(name):
            raise LLMOutputError(f"{name}: not available over MCP (no model of ours runs)")
        self.calls.clear()  # nothing reads them here; keep memory flat over a long sitting
        return super()._next(name, system, user)
