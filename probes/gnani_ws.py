"""Gnani wire-protocol details for the probes.

Since voice chunk V2 (ruling 24) the protocol code lives in `mockboard.speech.gnani_protocol`,
so the app's own Prisma service and these probes share one tested copy. This module
re-exports it under the old names (`from probes import gnani_ws as g`, then `g.STTConfig`,
`g.iter_frames`, ...) and keeps the one probe-only helper, `api_key`, which exits the process
with a readable message when the key is missing.

The docs each rule was copied from are listed in `mockboard/speech/gnani_protocol.py`.
"""

from __future__ import annotations

# probes/__init__.py puts src/ on sys.path when the package is not installed.
from mockboard.config import Settings, get_settings
from mockboard.speech.gnani_protocol import *  # noqa: F403  (re-export, see docstring)
from mockboard.speech.gnani_protocol import __all__ as _protocol_all

__all__ = [*_protocol_all, "Settings", "api_key", "get_settings"]


def api_key(settings: Settings | None = None) -> str:
    """Read the Gnani key from settings (env var GNANI_API_KEY or .env). Never logged."""
    s = settings or get_settings()
    if s.gnani_api_key is None or not s.gnani_api_key.get_secret_value().strip():
        raise SystemExit("GNANI_API_KEY is not set. Put it in .env or export it first.")
    key = s.gnani_api_key.get_secret_value()
    if key.startswith("replace-me"):
        raise SystemExit("GNANI_API_KEY still has the placeholder value from .env.example.")
    return key
