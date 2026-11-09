# CTO notes 18 — Mock Board as an MCP server ("bring your own AI")

Nov 8, 2026. Design for a first, local version. Owner's ask: "through an MCP the person's existing
LLM like Claude or ChatGPT is connected and then from there they won't have the need for any other
model". Status: built in `src/mockboard/mcp/`, offline only (Ruling 103).

## 0. What this is, and what it is not

- **It is a second channel.** The person's own assistant (Claude Desktop first) calls our tools.
  Their model reads the candidate's words, writes the analysis Nova's listener writes today, and
  speaks the board's lines in its own words. Our server runs only the deterministic parts.
- **It is not a replacement for the voice room.** MCP lets their assistant call us; it does not
  let our voice server call their model. (MCP "sampling" would, but most clients do not support
  it, so we do not rely on it.) The voice room keeps Nova, Prisma and Timbre.
- **It does not use Gnani.** The contest demo stays the Gnani voice room. This channel is text,
  or whatever voice the person's own app has. We name no Gnani model here because none runs.
- **No paid call of ours.** No Bedrock, no Gnani, no web fetch. The cost is the person's own
  assistant subscription.

## 1. The split: who does what

| Today (voice room) | Over MCP |
|---|---|
| Prisma hears the candidate | The person's app (typed text, or their app's own voice) |
| Nova listener (`listener.v6`, `record_panel_analysis`) | **Client model** fills `analysis` in `next_turn` (same fields) |
| Strategist rules, director, agenda, request rules (`requests.py` frozen, `think_time`, `requests_ta`), situations and twists, budgets, caps | **Ours, unchanged**: the real `InterviewSession` with the real panel hooks |
| Nova actor (one line per turn) | **Client model** speaks the line; we hand it the move, the seed question, our own draft, the persona and the limits |
| Fixed lines (hand-off, ack, repeat, think, twist, end confirm, close) | **Ours, said verbatim** by the client |
| Nova judge + citation check | **Client model** drafts scores; **our** existing citation check (`judge.check_citations`, `assemble_report`) keeps or drops each quote |
| Partner summary + quote check | **Client model** drafts notes; **our** `partner.report.check_citations` checks them |
| "How the board heard you", requests, report by member, discussion | **Ours** (code only, as today) |
| Timbre speaks | The person's app |

How the session runs without Nova: `mcp/client_brain.py` is an `LLMClient` that never calls a
model. Its listener reply is the deterministic word-list analysis with the client's analysis laid
over it (claims always empty: no fact cards, no fact challenges). Its actor reply is the
deterministic draft line (the same stand-in `--fake` uses), which our guards still check. Its
judge and partner-summary replies are the client's draft when one is given, otherwise an error, so
the existing code-only fallbacks run. Any other call name raises, so nothing silently pretends to
be a model. `offline = False`, so synthetic test cards can never enter (Ruling 36), and no news is
fetched (`news_search._live_client` is false). The agenda plan is off (Ruling 102 found it adds
almost nothing).

Trust: the client's `candidate_request` is treated exactly like Nova's listener vote (weak, only
on short turns; the phrase rules go first). The client cannot skip the director, change a budget,
or write a fixed line.

## 2. Tools

| Tool | In | Out |
|---|---|---|
| `start_interview` | `form` (the same fields as the site's form, no names), `max_answers` (optional, 3–20) | session id, members, the opening lines |
| `start_partner` | `topic`, `language` | session id, Mitra, the opening line |
| `next_turn` | `session_id`, `candidate_said` (exact words), `analysis` (optional, recommended), `you_said` (optional: what the assistant really said last) | the lines to say, in order, each marked `verbatim` or `own_words`; the board's decision (move, rule, request, counted, answers left, pending end confirmation); `finished` |
| `end_session` | `session_id` | the code-built report; the rubric (UPSC) or the notes shape (partner); every turn with its id, for quoting |
| `check_scores` | `session_id`, the client's draft (`scores`, `evidence`, optional `composure_moments`) | scores kept, quotes dropped, scores turned to `insufficient_evidence` |
| `check_notes` | `session_id`, the client's draft notes (`points`, `next_steps`) | points kept, fields emptied, problems |
| `likely_questions` | `form` | the template questions and hints (no model) |

One `next_turn` serves both modes (the session knows its mode); this is simpler for a client model
than two near-identical tools.

An `own_words` line carries: `move`, `question` (the agenda seed, when the move asks one),
`draft` (our wording; good enough to say as is), `max_words`, `language`, `avoid` (topics the
candidate refused) and plain `guidance`. A `verbatim` line must be said word for word.

## 3. Prompts and resources (MCP)

- Prompts: `upsc_board` (how to run the board), `study_partner`.
- Resources: `mockboard://guide/requests` (the phrases that always work, four languages; the same
  table as `/guide`), `mockboard://guide/honesty`, `mockboard://board/members`,
  `mockboard://guide/rubric`.
- The server's `instructions` string says the same in five lines.

## 4. Session state

In memory, in the server process (one process per client over stdio). At most 4 sessions; a
session idle for 3 hours is dropped. Ids are random (`mcp-` + 12 hex). One lock per session so two
tool calls cannot interleave a turn. Nothing is written to disk: the trace recorder is disabled,
and no transcript leaves the process except back to the client that sent it.

## 5. Privacy and safety

- The form has no name, roll number, phone, email or college (the same `CandidateForm`).
- Every free-text form field and the partner topic are refused when they hold a link, markup
  characters or an instruction phrase ("ignore…", "you are…", "system prompt", …). The checks are
  the partner topic rules plus the agenda's instruction words; messages name the field, never the
  value.
- `candidate_said` is capped at 4,000 characters, control characters removed. It is the
  candidate's own speech; our code does not follow instructions in it.
- Transcripts go to the person's own AI provider, under their terms; the guide resource and the
  owner instructions say so plainly. We store nothing.
- The honesty rules travel with every reply that asks the client to speak: no facts, figures or
  names of its own; ask, never assert; no praise or scores during the sitting; the members are
  fictional; synthetic practice, not a UPSC prediction.

## 6. Hard limits (say them, do not hide them)

1. Not the contest demo; no Gnani model runs here.
2. The client model's analysis and phrasing are outside our control; our guards check our drafts,
   not what the client finally says. The verbatim lines and the decisions are ours.
3. No fact checks (no real fact cards), no news briefing, no agenda plan.
4. No clock: the sitting ends by its answer budget, an end request or `end_session`, not 17 minutes.
5. The study partner still has no spoken end confirmation (Ruling 101), and none is added here.
6. ChatGPT needs a public HTTPS endpoint (remote MCP); not built, not deployed (§7).
7. The request detector's real-speech recall is still unmeasured (Ruling 57); typed text is easier.

## 7. Transport

The tool logic (`mcp/service.py`) has no MCP dependency and is tested directly. Two thin
transports read the same tool table:
- `mcp/stdio.py`: a small stdlib JSON-RPC 2.0 stdio server (newline-delimited messages:
  `initialize`, `tools/list`, `tools/call`, `prompts/list|get`, `resources/list|read`, `ping`).
  Works today with no new package.
- The official Python SDK (`mcp` on PyPI) as an optional extra, used only if the owner approves
  adding it (asked Nov 8; see Ruling 103).

ChatGPT later: its developer-mode connectors reach remote MCP servers over HTTPS (streamable HTTP),
so it needs a hosted endpoint with TLS and its own access control (the site's access limits). Not
in this chunk.

## 8. Addendum (Nov 8, later): Option 2, the MCP App, and the probe

The owner wants Gnani in this channel ("if it doesnt use gnani then its no use for now"), and chose
Option 2: the voice room as an **MCP App** (a panel inside the person's own Claude or ChatGPT chat).
Gnani Prisma listens and Timbre speaks, at our cost; the person's AI is the brain; §2's tools keep
the rules.

What the docs settle (MCP Apps spec 2026-01-26, TypeDoc; full note in the shared side-session
notes, `mcp-apps-feasibility.md`): the view is a `ui://` resource, `text/html;profile=mcp-app`,
pointed to by the tool's `_meta.ui.resourceUri`; it asks for the microphone with
`_meta.ui.permissions.microphone` (hosts MAY grant it) and for connections with
`_meta.ui.csp.connectDomains`; it speaks JSON-RPC over `postMessage` (`ui/initialize`,
`ui/notifications/initialized`, `ui/message`, `tools/call`). What they do not settle: whether
Claude Desktop shows panels from a local stdio server today (open bug anthropics/claude-ai-mcp#1069),
whether it grants the microphone, and whether localhost is allowed in `connectDomains`.

So a probe comes first: `python -m mockboard.mcp.probe` (`mcp/probe.py`, `mcp/probe_page.py`),
steps in `docs/MCP_PROBE.md`. It is free: no Gnani, no Bedrock, no outside host; its only
listener is 127.0.0.1. Its view was checked against a fake host in a browser (handshake, fetch,
WebSocket, `ui/message`, report all worked; the browser pane itself blocks microphones). Planned
design if the probe is green: the panel keeps its own WebSocket to our local server (mic audio to
Prisma; Timbre audio back from the AI's `say(session_id, member, text)` tool), and posts each
transcript with `ui/message`. If the panel does not render or gets no microphone: a local voice
page in the normal browser, driven by `say` and `wait_for_answer` (not built). Public hosting or a
tunnel needs the owner's approval.
