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


def test_stt_headers_use_gnanis_current_names_ruling_27():
    h = gp.build_stt_headers(gp.STTConfig(idle_timeout_s=300), "K")
    assert h["language_code"] == "en-IN" and h["format"] == "verbatim"
    assert h["model"] == "prisma-v2.5" and h["x-idle-timeout-sec"] == "300"
    assert not {"lang_code", "x-format", "x-model", "x-bias-list", "x-bias-score"} & set(h)


def test_v25_bias_needs_a_score_and_v3_uses_keyterms():
    h = gp.build_stt_headers(gp.STTConfig(bias_list=["UPSC"], bias_score=1.5), "K")
    assert json.loads(h["bias_list"]) == ["UPSC"] and h["bias_score"] == "1.5"
    for cfg in (
        gp.STTConfig(bias_list=["UPSC"]),
        gp.STTConfig(bias_list=["UPSC"], bias_score=0.0),
        gp.STTConfig(model="prisma-v3.0", bias_list=["UPSC"], bias_score=1.0),
    ):
        with pytest.raises(ValueError):
            gp.build_stt_headers(cfg, "K")
    v3 = gp.STTConfig(model="prisma-v3.0", keyterms=["Naxalism", "left wing extremism"],
                      topic="UPSC interview")
    h3 = gp.build_stt_headers(v3, "K")
    assert json.loads(h3["keyterms"]) == ["Naxalism", "left wing extremism"]
    assert h3["topic"] == "UPSC interview"
    # On v2.5 the v3.0-only context is simply not sent.
    h25 = gp.build_stt_headers(gp.STTConfig(keyterms=["Naxalism"], topic="x"), "K")
    assert "keyterms" not in h25 and "topic" not in h25


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": "prisma-v4"},
        {"fmt": "codemix"},
        {"fmt": "transliterate"},
        {"lang_code": "hi-IN,en-IN"},
        {"idle_timeout_s": 4},
        {"idle_timeout_s": 901},
    ],
)
def test_v3_only_values_and_ranges_are_refused_on_v25(kwargs):
    with pytest.raises(ValueError):
        gp.build_stt_headers(gp.STTConfig(**kwargs), "K")


def test_v3_codemix_with_a_language_list_is_accepted():
    cfg = gp.STTConfig(model="prisma-v3.0", fmt="codemix", lang_code="hi-IN,en-IN")
    assert gp.build_stt_headers(cfg, "K")["language_code"] == "hi-IN,en-IN"


def test_keyterms_are_cleaned_and_capped():
    assert gp.validate_keyterms([" UPSC ", "upsc", "", "left  wing"]) == ["UPSC", "left wing"]
    with pytest.raises(ValueError):
        gp.validate_keyterms([f"t{i}" for i in range(201)])
    assert json.loads(gp.update_keyterms_frame(["a b"])) == {
        "type": "update_settings", "bias": {"list": ["a b"]},
    }


def test_commit_frame_matches_the_docs():
    assert json.loads(gp.COMMIT_FRAME) == {"type": "commit"}
    assert "speech_start" in gp.KNOWN_STT_TYPES


def test_stt_config_reads_the_new_settings():
    from mockboard.config import Settings

    s = Settings(_env_file=None, stt_model="prisma-v3.0", stt_idle_timeout_s=120,
                 stt_min_silence_ms=300)
    cfg = gp.STTConfig.from_settings(s)
    assert cfg.model == "prisma-v3.0" and cfg.idle_timeout_s == 120
    assert cfg.min_silence_ms == 300 and cfg.vad_threshold == 0.7
    with pytest.raises(ValueError):
        Settings(_env_file=None, stt_model="prisma-v9")
