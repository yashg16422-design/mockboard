"""Mitra's spoken line: one streamed Nova call (`partner_actor`), then the partner's guards.

The model writes one line restating the speaker's latest point in fresh words, then exactly one
question for the move. Code then:
* drops a sentence that copies five or more words of the answer, stops at one that asks a quiz
  question (the board's `SentenceGate`, rulings 36 and 37);
* drops any sentence with a figure the speaker never said, and ends the line at its first
  question (`lines.clean_line`);
* finishes a line left without its question with the move's fixed question;
* answers a failed or late call with the move's fixed line in the session's language.
The clarify and "take your time" lines never reach the model (`session.py`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from mockboard.brain.actor import LineResult, SentenceGate, call_actor
from mockboard.brain.llm import LLMClient
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import BoardMove, Move
from mockboard.modes.partner.lines import (
    CLOSE_TARGET,
    asks,
    clean_line,
    template_line,
    template_question,
)
from mockboard.voice.lines import split_sentences

# The task for each move, written by code (English); the prompt explains the rules around it.
_TASKS: dict[str, str] = {
    "probe": "Ask for the reason behind their point: why do they think it holds?",
    "pressure": "Ask for one concrete example of their point, from something they know.",
    "counter": (
        "Offer the strongest counterpoint to their point, framed as what some would argue, "
        "never as a fact, then ask how they would answer it."
    ),
    "steelman": (
        "They granted the counterpoint. Ask them to restate their own point in its strongest "
        "form, with the counterpoint in mind."
    ),
    "reassure": (
        "They are stuck or said they do not know. Be warm and brief. Offer one gentle starting "
        "angle, a way to begin thinking, never the answer, then ask one easy question."
    ),
    "recap": (
        "Recap their points so far in one short sentence, in your own words, from "
        "<points_so_far>, then invite their next point."
    ),
    "close": (
        "Recap their points in one short sentence, in your own words, from <points_so_far>, "
        "then close warmly in one sentence. Ask no question."
    ),
}


def task_key(move: BoardMove) -> str:
    if move.move == Move.move_on:
        return "close" if move.target == CLOSE_TARGET else "recap"
    return move.move.value if move.move.value in _TASKS else "probe"


def needs_question(move: BoardMove) -> bool:
    return task_key(move) != "close"


def build_partner_user(
    move: BoardMove,
    *,
    mix: str,
    topic: str,
    previous_line: str,
    answer: str,
    note: str,
    points: Sequence[str],
    max_words: int,
) -> str:
    """Per-call content goes in the user message, each piece in its own tag: the speaker's words,
    the topic and the notes are data, never orders."""
    board = {
        "speaker": "Mitra",
        "move": move.move.value,
        "task": _TASKS[task_key(move)],
        "language_mix": mix,
        "max_words": max_words,
        "restate_first": task_key(move) not in ("recap", "close"),
        "ask_a_question": needs_question(move),
    }
    parts = [
        "Say Mitra's line now.",
        tag("partner_board", json.dumps(board, ensure_ascii=False)),
        tag("topic", topic),
        tag("previous_line", previous_line),
        tag("listener_note", note),
    ]
    if task_key(move) in ("recap", "close"):
        parts.append(tag("points_so_far", "\n".join(f"- {p}" for p in points if p.strip())))
    parts.append(tag("candidate_speech", answer))
    return "\n".join(parts)


def _fallback(move: BoardMove, mix: str, *, error: str, guard: str) -> LineResult:
    return LineResult(
        template_line(move.move, move.target, mix), is_fallback=True, guard=guard,
        fallback_kind="move_template", error=error,
    )


async def compose_partner_line(
    llm: LLMClient,
    kit: PromptKit,
    move: BoardMove,
    *,
    mix: str,
    topic: str,
    previous_line: str,
    answer: str,
    speech: str,
    note: str,
    points: Sequence[str],
    max_words: int,
    max_tokens: int,
    turn_id: str,
    prompt_id: str,
    on_first_token: Callable[[], None] | None = None,
    first_token_budget_s: float | None = None,
    fast_throttle_retry_window_s: float | None = None,
) -> LineResult:
    """The model line for `move`, with every guard. `speech` is everything the speaker has said
    this session (the figures a line may repeat); `answer` is their latest turn."""
    user = build_partner_user(
        move, mix=mix, topic=topic, previous_line=previous_line, answer=answer, note=note,
        points=points, max_words=max_words,
    )
    gate = SentenceGate(answer=answer, check_quotes=True, skip_quotes=True)
    call = await call_actor(
        llm, kit, user, max_words=max_words, max_tokens=max_tokens, turn_id=turn_id,
        on_first_token=on_first_token, first_token_budget_s=first_token_budget_s,
        fast_throttle_retry_window_s=fast_throttle_retry_window_s, prompt_id=prompt_id,
        gate=gate,
    )
    if call.error:
        return _fallback(move, mix, error=call.error, guard=call.guard)
    text, dropped = clean_line(call.text, speech)
    guard = call.guard or ("restate_dropped" if gate.skipped else "") or (
        "unsaid_figure" if dropped else "")
    if not needs_question(move):
        # The close asks nothing: keep the sentences before any question it wrote.
        text = " ".join(s for s in split_sentences(text) if not asks(s)).strip()
        if not text:
            return _fallback(move, mix, error="", guard=guard or "close_empty")
        return LineResult(text, guard=guard, retried=call.retried)
    if not text:
        return LineResult(template_line(move.move, move.target, mix), is_fallback=True,
                          guard=guard or "empty_line", fallback_kind="move_template",
                          retried=call.retried)
    if "?" not in text:
        question = template_question(move.move, move.target, mix) or ""
        return LineResult(f"{text} {question}".strip(), is_fallback=True,
                          guard=guard or "question_added", fallback_kind="move_template",
                          retried=call.retried)
    return LineResult(text, guard=guard, retried=call.retried)
