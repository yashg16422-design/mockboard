# MCP App probe — can Claude Desktop host the Gnani voice room? (free)

Option 2 puts Mock Board's voice room inside your own Claude chat as an "MCP App": a small panel
in which Gnani Prisma listens and Gnani Timbre speaks, while your Claude is the brain. Before we
build it, this probe answers five questions on your own Mac:

1. Does Claude Desktop show a panel from a **local** server? (A known bug, anthropics/claude-ai-mcp
   issue #1069, says local panels stopped showing in some builds.)
2. Does the panel get the **microphone**?
3. Can the panel reach Mock Board's server on this Mac (**fetch** and **WebSocket**)?
4. Can the panel **post a message into the chat** (how your spoken answer would reach Claude)?
5. Which Claude Desktop **version** this is.

**Cost:** nothing. No Gnani call, no Bedrock call, nothing on the internet. The probe opens the
microphone for a split second to see if it is allowed, then stops it. Nothing is recorded.

## Steps (your Mac)

1. Open Claude Desktop → **Settings** → **Developer** → **Edit Config**. This opens
   `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Add this block inside `mcpServers` (keep anything already there, for example `mockboard`):

```json
{
  "mcpServers": {
    "mockboard-probe": {
      "command": "/Users/yashpratapsingh/Projects/mockboard/.venv/bin/python",
      "args": ["-m", "mockboard.mcp.probe"]
    }
  }
}
```

3. Quit Claude Desktop completely (**Cmd+Q**) and open it again.
4. Open a **new chat** and type:

   > Call the mockboard-probe tool `mic_probe`. Then call `probe_result` and show me its text exactly.

5. If a microphone prompt appears (from Claude or from macOS), choose **Allow**. If macOS never asks
   and the mic shows denied, check **System Settings → Privacy & Security → Microphone → Claude**.
6. The panel fills in by itself in a few seconds. It also posts its readout into the chat.
   If the mic was denied the first time, press **Try the microphone again** once.

## What to paste back

Paste **both** of these to the Mock Board session:
- the panel's readout (the box that starts `MOCK BOARD PROBE v1`; the **Copy readout** button
  copies it), or the same text where it appeared in the chat;
- the text Claude shows from `probe_result` (it starts `MOCK BOARD PROBE v1 (server side)`).

How to read it:

| Line | Good | Means if not |
|---|---|---|
| `view rendered: yes` (only shown if the panel appears) | panel shows | issue #1069: see below |
| `ui handshake: ok` | the panel talks to Claude | panel shown but cut off from Claude |
| `mic (getUserMedia): granted` | the panel can hear you | Prisma would need the browser-tab fallback |
| `localhost fetch: ok`, `localhost websocket: ok` | the panel can stream to our server | same fallback |
| `ui/message to chat: sent` and the readout appears in the chat | your words can reach Claude | Claude would need a "wait for answer" tool |
| `host: …` | the Claude Desktop name and version | — |

## If no panel appears (issue #1069)

1. Look at `probe_result`:
   - "the host never fetched the view" → Claude Desktop did not even ask for the panel. This is
     the #1069 pattern.
   - "fetched but no report" → it asked, but the panel did not start or could not call back.
2. Try once each, then call `mic_probe` again:
   - Update Claude Desktop (menu **Claude → Check for Updates…**), quit with Cmd+Q, reopen.
   - An **incognito chat**: users on #1069 report panels showing there (Oct 15, unverified).
   - A brand-new chat, not one that already used other tools.
3. Still nothing: paste the `probe_result` text and your Claude Desktop version (**Claude → About
   Claude**). Server messages are usually in `~/Library/Logs/Claude/mcp-server-mockboard-probe.log`
   (UNVERIFIED path); paste its last lines too, they contain no secrets.
4. Do **not** set up a tunnel or a remote connector to get around it: that puts our server on the
   internet and needs your approval first.

## If port 8790 is busy

The probe tries 8790 to 8799 by itself. To pick another, add `"env": {"MOCKBOARD_PROBE_PORT":
"8800"}` next to `"args"` in the block above, then Cmd+Q and reopen.

## Afterwards

Remove the `mockboard-probe` block from the config and restart Claude Desktop. The probe keeps
nothing; its readout lives only in your chat.
