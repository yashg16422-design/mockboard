"""Lets Pipecat 1.5.0's SmallWebRTC transport import without OpenCV (defect E).

`pipecat.transports.smallwebrtc.transport` does `import cv2` at module level, and the
`pipecat-ai[webrtc]` extra installs `opencv-python` for it. OpenCV is used there only to
convert incoming VIDEO frames to RGB (`SmallWebRTCClient.FORMAT_CONVERSIONS` and
`cv2.cvtColor`); our app receives audio only (`video_in_enabled` stays False). On macOS the
opencv-python wheel and PyAV (aiortc's media library) each bundle their own FFmpeg
`libavdevice`, and the Objective-C runtime warns that both define the same classes ("may
cause spurious casting failures and mysterious crashes"; seen in the owner's first run).

So the voice extra no longer installs OpenCV (it depends on `aiortc` directly instead of
`pipecat-ai[webrtc]`), and `ensure_cv2()` registers this small stand-in before the transport
is imported. The stand-in has the four colour constants the transport reads at class
definition and a `cvtColor` that refuses to run, so if video were ever turned on it fails
loudly instead of silently. If a real OpenCV is installed, it is used and nothing changes.
Checked against the installed pipecat-ai 1.5.0 source (transport.py lines 48 and 219-292).
"""

from __future__ import annotations

import importlib.util
import sys
import types

# The values do not matter (they are only dictionary values until cvtColor is called);
# these are OpenCV's own, for readability in a debugger.
_CONSTANTS = {
    "COLOR_YUV2RGB_I420": 99,
    "COLOR_YUV2RGB_NV12": 90,
    "COLOR_GRAY2RGB": 8,
}


def _cvt_color(*_args, **_kwargs):
    raise RuntimeError(
        "OpenCV is not installed: Mock Board's voice app takes audio only. Video input needs "
        "`pip install opencv-python` (beware its libavdevice clash with PyAV on macOS)."
    )


def ensure_cv2() -> bool:
    """Install the stand-in `cv2` if no real OpenCV is importable. Returns True if it did."""
    if "cv2" in sys.modules or importlib.util.find_spec("cv2") is not None:
        return False
    stub = types.ModuleType("cv2")
    stub.__doc__ = "Mock Board stand-in for OpenCV (audio-only SmallWebRTC); see cv2_shim."
    for name, value in _CONSTANTS.items():
        setattr(stub, name, value)
    stub.cvtColor = _cvt_color
    stub.MOCKBOARD_STUB = True
    sys.modules["cv2"] = stub
    return True
