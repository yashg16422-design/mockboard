"""`python -m mockboard.mcp`: Mock Board's MCP server on stdio, for Claude Desktop.

Makes no paid call: no Bedrock, no Gnani, no web fetch (CTO_NOTES_18, Ruling 103).
"""

from __future__ import annotations

from mockboard.mcp.stdio import run_stdio

if __name__ == "__main__":
    run_stdio()
