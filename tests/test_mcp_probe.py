"""The MCP App probe (`python -m mockboard.mcp.probe`, Option 2 checkpoint). Offline: a fake MCP
client and a real socket on 127.0.0.1 only. No Gnani, Bedrock or outside host."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import pytest

from mockboard.mcp.probe import (
    APP_MIME,
    UI_EXTENSION,
    VIEW_URI,
    ProbeServer,
    read_text_frame,
    text_frame,
)


async def _rpc(server: ProbeServer, method: str, params: dict | None = None, rid: int = 1) -> Any:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    resp = await server.handle(json.loads(json.dumps(msg)))
    json.dumps(resp)
    return resp


@pytest.fixture
async def probe():
    server = ProbeServer(port=int(os.environ.get("MOCKBOARD_TEST_PROBE_PORT", "8890")))
    await server.start()
    assert server.port
    yield server
    await server.aclose()


async def test_initialize_declares_the_ui_extension_and_records_the_client(probe):
    resp = await _rpc(probe, "initialize", {
        "protocolVersion": "2025-06-18",
        "clientInfo": {"name": "claude-ai", "version": "9.9"},
        "capabilities": {"extensions": {UI_EXTENSION: {"mimeTypes": [APP_MIME]}}},
    })
    result = resp["result"]
    assert result["capabilities"]["extensions"][UI_EXTENSION]["mimeTypes"] == [APP_MIME]
    assert result["serverInfo"]["name"] == "mockboard-probe"
    text = probe.result_text()
    assert "client (MCP initialize): claude-ai 9.9" in text
    assert "client declares the ui extension: yes" in text
    assert "the host never fetched the view" in text


async def test_the_probe_tool_points_at_its_view_and_the_report_tool_is_app_only(probe):
    tools = {t["name"]: t for t in (await _rpc(probe, "tools/list"))["result"]["tools"]}
    assert set(tools) == {"mic_probe", "probe_report", "probe_result"}
    assert tools["mic_probe"]["_meta"]["ui"]["resourceUri"] == VIEW_URI
    assert tools["mic_probe"]["_meta"]["ui/resourceUri"] == VIEW_URI  # older hosts
    assert tools["probe_report"]["_meta"]["ui"]["visibility"] == ["app"]
    assert "_meta" not in tools["probe_result"]


async def test_the_view_is_an_app_resource_with_mic_and_localhost_declared(probe):
    listed = (await _rpc(probe, "resources/list"))["result"]["resources"]
    assert listed[0]["uri"] == VIEW_URI and listed[0]["mimeType"] == APP_MIME
    read = (await _rpc(probe, "resources/read", {"uri": VIEW_URI}))["result"]["contents"][0]
    assert read["mimeType"] == APP_MIME and read["uri"] == VIEW_URI
    ui = read["_meta"]["ui"]
    assert ui["permissions"] == {"microphone": {}}
    port = probe.port
    assert f"http://127.0.0.1:{port}" in ui["csp"]["connectDomains"]
    assert f"ws://127.0.0.1:{port}" in ui["csp"]["connectDomains"]
    html = read["text"]
    assert f'const PORT = "{port}"' in html and "__PORT__" not in html and "__VERSION__" not in html
    for method in ("ui/initialize", "ui/notifications/initialized", "ui/message", "tools/call",
                   "probe_report", "getUserMedia"):
        assert method in html, method
    assert "https://" not in html and "<script src" not in html  # no outside host
    assert "stop()" in html  # the mic stream is stopped at once
    assert probe.view_reads == 1
    bad = await _rpc(probe, "resources/read", {"uri": "ui://other/x.html"})
    assert bad["error"]["code"] == -32602


async def test_the_panel_report_reaches_probe_result(probe):
    readout = "MOCK BOARD PROBE v1\nmic (getUserMedia): granted"
    await _rpc(probe, "resources/read", {"uri": VIEW_URI})
    stored = await _rpc(probe, "tools/call",
                        {"name": "probe_report", "arguments": {"readout": readout}})
    assert stored["result"]["content"][0]["text"] == "stored"
    got = await _rpc(probe, "tools/call", {"name": "probe_result", "arguments": {}})
    text = got["result"]["content"][0]["text"]
    assert "panel readout:" in text and "mic (getUserMedia): granted" in text
    shown = await _rpc(probe, "tools/call", {"name": "mic_probe", "arguments": {}})
    assert "probe_result" in shown["result"]["content"][0]["text"]
    assert (await _rpc(probe, "tools/call", {"name": "nope"}))["error"]["code"] == -32602


async def test_fetched_but_silent_view_is_reported_as_such(probe):
    await _rpc(probe, "resources/read", {"uri": VIEW_URI})
    assert "fetched but no report" in probe.result_text()


async def test_the_echo_endpoint_answers_ping_with_cors_and_refuses_other_paths(probe):
    reader, writer = await asyncio.open_connection("127.0.0.1", probe.port)
    writer.write(b"GET /ping HTTP/1.1\r\nHost: x\r\nOrigin: null\r\n\r\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    assert data.startswith(b"HTTP/1.1 200") and data.endswith(b"pong")
    assert b"Access-Control-Allow-Origin: *" in data
    reader, writer = await asyncio.open_connection("127.0.0.1", probe.port)
    writer.write(b"GET /etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    assert (await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 404")
    writer.close()
    assert probe.http_hits == 1


async def test_the_echo_websocket_answers_ping_with_pong(probe):
    reader, writer = await asyncio.open_connection("127.0.0.1", probe.port)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((f"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert head.startswith(b"HTTP/1.1 101")
    mask = os.urandom(4)
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(b"ping"))
    writer.write(bytes([0x81, 0x80 | 4]) + mask + payload)  # a masked client text frame
    await writer.drain()
    assert await asyncio.wait_for(read_text_frame(reader), 5) == "pong"
    writer.close()
    assert probe.ws_hits == 1


def test_frames_are_short_text_only():
    assert text_frame("pong") == b"\x81\x04pong"
    with pytest.raises(ValueError):
        text_frame("x" * 200)


async def test_a_busy_port_moves_to_the_next_one():
    first = ProbeServer(port=8895)
    await first.start()
    second = ProbeServer(port=first.port)
    await second.start()
    try:
        assert second.port != first.port and second.port
    finally:
        await first.aclose()
        await second.aclose()
