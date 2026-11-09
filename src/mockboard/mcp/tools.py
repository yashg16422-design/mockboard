"""The MCP surface: tool schemas, prompts and resources, and one dispatcher (CTO_NOTES_18 §2, §3).

Transport-free: `stdio.py` (stdlib) and an SDK adapter can both serve this table. Schemas are
hand-written JSON Schema, kept flat and small so any client model can fill them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mockboard.brain.schemas import Dimension
from mockboard.mcp import guidance
from mockboard.mcp.service import SCORING_STEPS, BoardService, ToolError
from mockboard.modes.partner.session import LANGUAGES as PARTNER_LANGUAGES

LANGUAGES = ["hinglish", "english", "hindi", "tanglish"]
REQUESTS = ["none", "change_topic", "repeat", "clarify_question", "skip", "end"]

FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "The short application form. No name, roll number, phone, email or college.",
    "properties": {
        "home_state": {"type": "string", "description": "Home state, e.g. 'Uttar Pradesh'."},
        "district": {"type": "string", "description": "Optional home district."},
        "degree_subject": {"type": "string", "description": "Subject of the degree."},
        "hobbies": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "optional_subject": {"type": "string", "description": "Optional UPSC subject."},
        "work_experience": {"type": "string", "description": "Optional, one short line."},
        "interview_language": {"type": "string", "enum": LANGUAGES},
    },
    "required": ["home_state", "degree_subject"],
    "additionalProperties": False,
}

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Your reading of the candidate's answer (what Mock Board's own listener records). "
        "Recommended: without it Mock Board falls back to simple word rules."
    ),
    "properties": {
        "stance": {"type": "string", "description": "Their position, one short English sentence."},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                    "description": "Each distinct reason they gave, a few words each."},
        "balance": {"type": "string", "enum": ["balanced", "one_sided", "unclear"]},
        "honest_unknown": {"type": "boolean", "description": "They said they do not know."},
        "concedes_error": {"type": "boolean", "description": "They corrected themselves."},
        "garbled": {"type": "boolean", "description": "The words make no sense as speech."},
        "candidate_request": {
            "type": "string", "enum": REQUESTS,
            "description": "What they asked the board to do, if anything; otherwise 'none'.",
        },
    },
    "additionalProperties": False,
}

_SESSION = {"type": "string", "description": "The session_id from the start tool."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "start_interview",
        "title": "Start a UPSC mock interview",
        "description": (
            "Start a practice UPSC Personality Test with Mock Board's five fictional members. "
            "Returns the members and the opening lines to say."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "form": FORM_SCHEMA,
                "max_answers": {"type": "integer", "minimum": 5, "maximum": 20,
                                "description": "Optional shorter sitting: answers before the "
                                               "close."},
            },
            "required": ["form"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_partner",
        "title": "Start a study-partner session",
        "description": (
            "Start a low-pressure study session with Mitra on one topic the person is studying. "
            "No scores. Returns the opening line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "maxLength": 80,
                          "description": "A subject name only, e.g. 'Federalism in India'."},
                "language": {"type": "string", "enum": list(PARTNER_LANGUAGES)},
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
    },
    {
        "name": "next_turn",
        "title": "Send the answer, get the board's next lines",
        "description": (
            "Call after every answer. Send the person's exact words; Mock Board decides the move "
            "(follow-up, next member, repeat, time to think, change topic, end with confirmation) "
            "and returns the lines to say, each 'verbatim' or 'own_words'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": _SESSION,
                "candidate_said": {"type": "string", "maxLength": 4000,
                                   "description": "Their exact words. Empty string for silence."},
                "analysis": ANALYSIS_SCHEMA,
                "you_said": {"type": "string", "maxLength": 4000,
                             "description": "What you actually said for the last board line."},
            },
            "required": ["session_id", "candidate_said"],
            "additionalProperties": False,
        },
    },
    {
        "name": "end_session",
        "title": "End the session and get the report",
        "description": (
            "End the sitting (or collect the report after it finished). Returns the report built "
            "by Mock Board's code, every turn with its id, and the rubric for scoring."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": _SESSION},
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_scores",
        "title": "Quote-check draft interview scores",
        "description": (
            "After end_session on an interview: send your draft scores for all nine dimensions "
            "with word-for-word evidence quotes. Mock Board drops quotes not found in the cited "
            "turn and turns unsupported scores into 'insufficient_evidence'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": _SESSION,
                "scores": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "dimension": {"type": "string", "enum": [d.value for d in Dimension]},
                            "score": {"type": ["integer", "string"],
                                      "description": "1-5, or 'insufficient_evidence'."},
                            "rationale": {"type": "string", "maxLength": 300},
                        },
                        "required": ["dimension", "score", "rationale"],
                    },
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "dimension": {"type": "string", "enum": [d.value for d in Dimension]},
                            "turn_id": {"type": "string"},
                            "quote": {"type": "string",
                                      "description": "The candidate's exact words in that turn."},
                        },
                        "required": ["dimension", "turn_id", "quote"],
                    },
                },
                "composure_moments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"turn_id": {"type": "string"}, "note": {"type": "string"}},
                        "required": ["turn_id", "note"],
                    },
                },
            },
            "required": ["session_id", "scores", "evidence"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_notes",
        "title": "Quote-check draft study notes",
        "description": (
            "After end_session on a study-partner session: send your draft notes. Each point, "
            "reason, example and answer must quote the speaker; a counterpoint must quote Mitra."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": _SESSION,
                "points": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            k: {"type": "string"}
                            for k in ("point", "reason", "example", "counterpoint", "answer",
                                      "open")
                        },
                        "required": ["point"],
                    },
                },
                "next_steps": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            },
            "required": ["session_id", "points"],
            "additionalProperties": False,
        },
    },
    {
        "name": "likely_questions",
        "title": "Likely interview questions",
        "description": (
            "Questions the board may ask, from the form alone, each with a short hint on how to "
            "approach it. Written by templates, not a model."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"form": FORM_SCHEMA},
            "required": ["form"],
            "additionalProperties": False,
        },
    },
]

# --- the Gnani voice room (Option 2, `voice_bridge.py`) ------------------------------------------

VIEW_URI = "ui://mockboard/voice-room.html"
APP_MIME = "text/html;profile=mcp-app"
_UI = {"ui": {"resourceUri": VIEW_URI}, "ui/resourceUri": VIEW_URI}  # current and older key
_SID = {"type": "string", "description": "The session_id from the start tool."}
_PLAN_LINE = {
    "type": "object",
    "properties": {
        "member": {"type": "string", "description": "The member's id from `members`."},
        "angle": {"type": "string", "maxLength": 160,
                  "description": "The line of questioning, in a few words or one question."},
        "why": {"type": "string", "maxLength": 160,
                "description": "Why this member will ask it, from the form."},
        "weak_spot": {"type": "string", "maxLength": 120,
                      "description": "What you expect the person may find hard."},
    },
    "required": ["member", "angle", "why"],
    "additionalProperties": False,
}
TOOLS += [
    {
        "name": "start_voice_interview",
        "title": "Start a spoken UPSC interview (Gnani voice room)",
        "description": (
            "Like start_interview, but spoken: Gnani Prisma hears the person and Gnani Timbre "
            "speaks each member. Opens the voice room (a panel here, or voice_url in a browser). "
            "Then call prepare_board, say, wait_for_answer and next_turn."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "form": FORM_SCHEMA,
                "max_answers": {"type": "integer", "minimum": 5, "maximum": 20},
            },
            "required": ["form"],
            "additionalProperties": False,
        },
        "_meta": _UI,
    },
    {
        "name": "start_voice_partner",
        "title": "Start a spoken study session with Mitra (Gnani voice room)",
        "description": "Like start_partner, but spoken through Gnani's speech models.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "maxLength": 80},
                "language": {"type": "string", "enum": list(PARTNER_LANGUAGES)},
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
        "_meta": _UI,
    },
    {
        "name": "prepare_board",
        "title": "Show the board's preparation",
        "description": (
            "Before the first question: each member's lines of questioning from the form, with "
            "why and the weak spot to probe (at most three a member; no figures, links or "
            "markup). Mock Board checks them and the voice room draws them as an animated graph."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": _SID,
                           "plan": {"type": "array", "items": _PLAN_LINE, "maxItems": 18}},
            "required": ["session_id", "plan"],
            "additionalProperties": False,
        },
    },
    {
        "name": "say",
        "title": "Speak the board's lines (Gnani Timbre)",
        "description": (
            "Speak lines in the voice room, each in its member's Timbre voice, in order. Returns "
            "when they have been played. Then call wait_for_answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": _SID,
                "lines": {
                    "type": "array", "minItems": 1, "maxItems": 6,
                    "items": {"type": "object",
                              "properties": {"member": {"type": "string"},
                                             "text": {"type": "string", "maxLength": 1200}},
                              "required": ["member", "text"], "additionalProperties": False},
                },
            },
            "required": ["session_id", "lines"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_for_answer",
        "title": "Wait for the person's spoken answer (Gnani Prisma)",
        "description": (
            "Returns what Gnani Prisma heard when the person finishes speaking (or a typed "
            "answer): status `answered` or `silence` with `candidate_said`, or `waiting` after "
            "about fifty seconds, when you call it again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": _SID,
                           "timeout_s": {"type": "number", "minimum": 5, "maximum": 50}},
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "voice_room_ticket",
        "title": "Voice room connection (for the panel)",
        "description": "Used by the voice room panel only: how to reach the latest voice room.",
        "inputSchema": {"type": "object", "properties": {}, "required": [],
                        "additionalProperties": False},
        "_meta": {"ui": {"visibility": ["app"]}},
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)

PROMPTS: list[dict[str, Any]] = [
    {"name": "upsc_board", "title": "Run a Mock Board UPSC interview",
     "description": "How to run the board: the steps, the honesty rules, the phrases."},
    {"name": "study_partner", "title": "Study with Mitra",
     "description": "How to run a study-partner session with Mock Board."},
    {"name": "voice_room", "title": "Run Mock Board spoken (Gnani voice room)",
     "description": "How to run the board or Mitra by voice: Gnani Prisma hears, Timbre speaks."},
]

RESOURCES: list[dict[str, Any]] = [
    {"uri": "mockboard://guide/requests", "name": "requests", "title": "Things you can say",
     "description": "Request phrases that always work, in four languages.",
     "mimeType": "text/plain"},
    {"uri": "mockboard://guide/honesty", "name": "honesty", "title": "Honesty and privacy",
     "description": "The rules the assistant follows, what is stored, and the limits.",
     "mimeType": "text/plain"},
    {"uri": "mockboard://board/members", "name": "members", "title": "The board's members",
     "description": "The five fictional board members and Mitra.",
     "mimeType": "application/json"},
    {"uri": "mockboard://guide/rubric", "name": "rubric", "title": "Report rubric",
     "description": "The nine report dimensions and how scores are checked.",
     "mimeType": "application/json"},
    {"uri": VIEW_URI, "name": "voice-room", "title": "Mock Board voice room",
     "description": "The voice room view (MCP App): the room, captions and the decision graph.",
     "mimeType": APP_MIME},
]


class UnknownName(KeyError):
    """No tool, prompt or resource by that name: a protocol error, not a tool error."""


def prompt_text(name: str) -> str:
    if name == "upsc_board":
        return guidance.upsc_prompt()
    if name == "study_partner":
        return guidance.partner_prompt()
    if name == "voice_room":
        return guidance.voice_prompt()
    raise UnknownName(name)


def resource_text(service: BoardService, uri: str) -> str:
    if uri == "mockboard://guide/requests":
        return guidance.phrases_text()
    if uri == "mockboard://guide/honesty":
        return guidance.honesty_text()
    if uri == "mockboard://board/members":
        return json.dumps(service.board_members(), ensure_ascii=False, indent=1)
    if uri == "mockboard://guide/rubric":
        return json.dumps({"rubric": service.rubric(), "how_to_score": SCORING_STEPS},
                          ensure_ascii=False, indent=1)
    raise UnknownName(uri)


async def call_tool(service: BoardService, name: str, args: Mapping[str, Any] | None
                    ) -> tuple[dict[str, Any], bool]:
    """(result, is_error). A `ToolError` becomes an error result the model can read and fix."""
    if name not in TOOL_NAMES:
        raise UnknownName(name)
    a = dict(args or {})
    try:
        if name == "start_interview":
            result = await service.start_interview(a.get("form"), a.get("max_answers"))
        elif name == "start_partner":
            result = await service.start_partner(a.get("topic"), a.get("language", "hinglish"))
        elif name == "next_turn":
            result = await service.next_turn(a.get("session_id"), a.get("candidate_said"),
                                             a.get("analysis"), a.get("you_said"))
        elif name == "end_session":
            result = await service.end_session(a.get("session_id"))
        elif name == "check_scores":
            result = await service.check_scores(a.get("session_id"), a.get("scores"),
                                                a.get("evidence"), a.get("composure_moments"))
        elif name == "check_notes":
            result = await service.check_notes(a.get("session_id"), a.get("points"),
                                               a.get("next_steps"))
        elif name == "start_voice_interview":
            result = await service.start_voice_interview(a.get("form"), a.get("max_answers"))
        elif name == "start_voice_partner":
            result = await service.start_voice_partner(a.get("topic"),
                                                       a.get("language", "hinglish"))
        elif name == "prepare_board":
            result = await service.prepare_board(a.get("session_id"), a.get("plan"))
        elif name == "say":
            result = await service.say(a.get("session_id"), a.get("lines"))
        elif name == "wait_for_answer":
            result = await service.wait_for_answer(a.get("session_id"), a.get("timeout_s"))
        elif name == "voice_room_ticket":
            result = service.voice_ticket()
        else:
            result = service.likely_questions(a.get("form"))
    except ToolError as exc:
        return {"error": str(exc)}, True
    return result, False
