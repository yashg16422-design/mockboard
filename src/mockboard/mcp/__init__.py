"""Mock Board as an MCP server ("bring your own AI", CTO_NOTES_18, Ruling 103).

The person's own assistant is the model; this package runs only Mock Board's deterministic code.
`service.BoardService` holds the tools, `tools` their schemas, `stdio` a stdlib transport, and
`python -m mockboard.mcp` starts it for Claude Desktop.
"""
