"""The shared Gnani protocol module (moved from probes/gnani_ws.py in V2). No Pipecat needed,
so these run in `make test` on a plain checkout."""

from __future__ import annotations

import json

import pytest
from probes import gnani_ws

from mockboard.speech import gnani_protocol as gp


def test_frame_buffer_turns_20ms_input_into_exact_1024_byte_frames():
    buf = gp.FrameBuffer()
    out: list[bytes] = []
    for _ in range(8):  # eight 640-byte (20 ms) frames = 5,120 bytes
        out += buf.push(b"\x01\x02" * 320)
    assert [len(f) for f in out] == [1024] * 5
    assert len(buf) == 0
    assert buf.push(b"\x00" * 100) == [] and len(buf) == 100


def test_frame_buffer_flush_pads_with_silence_once():
    buf = gp.FrameBuffer()
    buf.push(b"\x7f" * 10)
    frame = buf.flush()
    assert frame is not None and len(frame) == 1024
    assert frame[:10] == b"\x7f" * 10 and frame[10:] == b"\x00" * 1014
    assert buf.flush() is None


def test_frame_buffer_keeps_byte_order_across_pushes():
    buf = gp.FrameBuffer(frame_bytes=4)
    assert buf.push(b"ab") == []
    assert buf.push(b"cdefg") == [b"abcd"]
    assert buf.push(b"h") == [b"efgh"]


def test_probes_still_see_the_same_protocol_objects():
    # The probes import `gnani_ws as g` and use g.<name>; it must be the moved code itself.
    for name in gp.__all__:
        assert getattr(gnani_ws, name) is getattr(gp, name), name
    assert callable(gnani_ws.api_key)


def test_stt_headers_hold_the_key_but_redaction_hides_it():
    cfg = gp.STTConfig(min_silence_ms=300, vad_threshold=0.5)
    headers = gp.build_stt_headers(cfg, "k-SECRET")
    assert headers["x-api-key-id"] == "k-SECRET"
    assert "k-SECRET" not in json.dumps(gp.redact_headers(headers))


def test_update_settings_frame_matches_the_docs_example():
    assert json.loads(gp.update_settings_frame(["Vachana", "EMI"], 1.5)) == {
        "type": "update_settings",
        "bias": {"list": ["Vachana", "EMI"], "score": 1.5},
    }
    with pytest.raises(ValueError):
        gp.update_settings_frame(["two words"])
