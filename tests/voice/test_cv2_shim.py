"""Defect E (Round 19): the voice app imports Pipecat's SmallWebRTC transport without OpenCV.
The shim itself needs no Pipecat; the transport import is checked only with the voice extra."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = """
import sys
from mockboard.voice.cv2_shim import ensure_cv2
real = __import__("importlib").util.find_spec("cv2") is not None
installed = ensure_cv2()
assert installed is (not real)
import cv2
if installed:
    assert cv2.MOCKBOARD_STUB and cv2.COLOR_GRAY2RGB == 8
    try:
        cv2.cvtColor(None, 8)
    except RuntimeError as e:
        assert "audio only" in str(e)
    else:
        raise AssertionError("the stand-in must refuse to convert video")
assert ensure_cv2() is False  # idempotent
if "--transport" in sys.argv:
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCClient
    assert set(SmallWebRTCClient.FORMAT_CONVERSIONS) >= {"yuv420p", "nv12", "gray"}
print("ok", installed)
"""


SRC = Path(__file__).resolve().parents[2] / "src"


def _run(*args: str) -> str:
    # The child must find this checkout's package even when pytest's pythonpath is not set.
    path = os.pathsep.join(p for p in (str(SRC), os.environ.get("PYTHONPATH", "")) if p)
    out = subprocess.run([sys.executable, "-c", SCRIPT, *args], capture_output=True, text=True,
                         timeout=120, env={**os.environ, "PYTHONPATH": path})
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_the_stand_in_is_installed_only_without_a_real_opencv():
    assert _run().startswith("ok")


def test_the_smallwebrtc_transport_imports_with_the_stand_in():
    if importlib.util.find_spec("pipecat") is None or importlib.util.find_spec("aiortc") is None:
        pytest.skip("needs the voice extra")
    assert _run("--transport").startswith("ok")


def test_the_voice_extra_does_not_pull_opencv():
    import tomllib

    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    voice = pyproject["project"]["optional-dependencies"]["voice"]
    assert not any("webrtc" in d or "opencv" in d for d in voice), voice
    assert "aiortc==1.15.0" in voice
