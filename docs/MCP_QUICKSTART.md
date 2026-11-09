# Mock Board in your own AI (MCP) — quickstart

Mock Board can run inside an AI assistant you already use. Your assistant's model does the
talking; Mock Board's code decides every move (who asks, follow-ups, repeat, time to think, change
of topic, end with confirmation, the DM situation and its twist) and builds the report. Design:
`docs/CTO_NOTES_18.md`. Ruling 103.

**What it costs:** nothing on Mock Board's side. No Bedrock, no Gnani, no web call. Your
assistant's own subscription does the work.
**What it is not:** the voice room. No Gnani model runs here, so the contest demo stays
`make voice-upsc`.

## 1. Check it works (your Mac, repo folder, inside `.venv`)

```bash
cd ~/Projects/mockboard
```

```bash
source .venv/bin/activate
```

```bash
python -m pytest -q tests/test_mcp_server.py
```

All tests should pass. They are offline and free.

## 2. Add it to Claude Desktop (your Mac)

1. Open Claude Desktop → **Settings** → **Developer** → **Edit Config**. This opens
   `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Add Mock Board under `mcpServers` (keep any servers already there):

```json
{
  "mcpServers": {
    "mockboard": {
      "command": "/Users/yashpratapsingh/Projects/mockboard/.venv/bin/python",
      "args": ["-m", "mockboard.mcp"]
    }
  }
}
```

   The `command` must be the full path to the project's own Python, because Mock Board is
   installed in that `.venv`. No `cwd` and no environment variables are needed: the server reads
   no `.env` and needs no key.
3. Quit Claude Desktop completely (Cmd+Q) and open it again.
4. In a new chat, open the tools menu: **mockboard** should list 13 tools (one, `voice_room_ticket`, is for the voice room panel and may be hidden).

## 3. Use it

Type, for example:

> Use Mock Board to give me a UPSC mock interview. Read its "upsc_board" prompt first.

Claude asks for the short form (home state, degree subject, hobbies, language; never your name),
calls `start_interview`, and speaks for the board. After each of your answers it calls
`next_turn`. Say "May I take a moment?", "Could you repeat the question?", "Could we move to
another topic?" or "Can we end the interview here?" to test the requests. At the end it calls
`end_session` for the report; if it scores you, it must pass `check_scores`, which drops any quote
you did not really say.

For the study partner:

> Use Mock Board's study partner on "Federalism in India", in English.

The prompts appear in Claude Desktop's attachment ("+") menu as **upsc_board** and
**study_partner**, if you prefer to attach them.

## 4. Privacy, said plainly

- Mock Board's server keeps sessions in memory only and writes no file. Closing Claude Desktop
  ends them.
- Your answers go to the AI provider of the assistant you use (here, Anthropic), under its terms.
  Use practice answers, not anyone's real details.
- The form refuses names, phone numbers and other identifiers, links, markup and instruction-like
  text.

## 4b. Spoken: the Gnani voice room (Option 2, Nov 9)

The same board, spoken. **Gnani Prisma** (`prisma-v2.5`) hears you and **Gnani Timbre**
(`timbre-v2.5`) gives each member a voice; your Claude words the board; Mock Board's code decides
every move. Gnani is billed to the project's own key (the `.env` in this folder), so the server
now reads that file; the key is never shown or sent anywhere but Gnani.

1. Use the same `mockboard` entry as in step 2 (no change needed). Quit and reopen Claude Desktop
   so it loads the new tools.
2. In a new chat, type:

   > Use Mock Board's voice room for a UPSC mock interview. Read its "voice_room" prompt first.

3. Claude asks for the short form, then calls `start_voice_interview`. **Where the room appears:**
   - as a **panel inside the chat**, if Claude Desktop shows Mock Board's view; or
   - if no panel appears, ask Claude for the **`voice_url`** and open it in Chrome on this Mac.
4. Watch **the board prepare**: your form's facts, the five members and the lines each will pursue
   (with the weak spot they expect) draw themselves. The board starts speaking a few seconds after
   the drawing finishes.
5. Press **Start microphone** (this also lets the voices play). Answer aloud; a pause of about a
   second and a half ends your answer (or press **I have finished**). You can also type.
6. The right side shows **how the board is deciding**: one node per decision in each member's
   lane, and a card with the rule, the reason, and what was found in your answer (unsure, very
   short, no reason). **The board's preparation** brings the drawing back at any time.
7. Say "Can we end the interview here?" and then "Yes" to finish; Claude shows the report.

For the study partner: "Use Mock Board's voice room to study Federalism in India with Mitra."

**What was checked (Nov 9):** the whole path with fake speech in tests (free), the page in a
browser at laptop width, and one small paid round trip: Timbre spoke the opening (8.2 s of audio,
in the Chairperson's voice) and Prisma's transcript of a generated 8-second Hinglish answer came
back through `wait_for_answer`, 2.1 s after the turn ended in that test (the clip had no pause
before the end; with a real pause it should be sooner, unmeasured). **Not checked:** whether
Claude Desktop shows the panel and grants it the microphone (the probe in `MCP_PROBE.md`
measures that); your own voice; Tamil.

## 5. Limits

- Your assistant phrases the `own_words` lines; Mock Board cannot check what it finally says. The
  decisions and the `verbatim` lines are Mock Board's.
- No fact checks, no news briefing, no 17-minute clock (the sitting ends by its answer budget or
  an end request).
- The study partner ends at once when asked (no confirmation), as in the voice room.
- If your assistant skips `next_turn` and improvises, it is no longer Mock Board's board.

## 6. ChatGPT and claude.ai (web) — not built

Both connect only to a **remote** MCP server: a public HTTPS address, not a program on your Mac.
That needs, in a later chunk and with your approval:

1. An HTTP transport (MCP "streamable HTTP"); the official Python SDK provides one.
2. Hosting with TLS, and access control (the site's access limits), since anyone with the URL
   could use it.
3. A privacy review of where session memory lives on a server.

Then: ChatGPT → Settings → Connectors → developer mode → add the URL; claude.ai → Settings →
Connectors → add custom connector → the URL. UNVERIFIED: the exact menu names on the day; check
each product's help page then. Nothing is deployed today.

## 7. Optional: the official MCP SDK

The server above uses only Python's standard library. The official Python SDK (`mcp` on PyPI) is
not installed; adding it needs your approval (asked Nov 8). It would mainly matter for the HTTPS
version in §6.
