"""Langfuse stub, behind `LANGFUSE_ENABLED` (default false). No hard dependency.

ENGINEERING.md §8 records the documented LangGraph integration as
`from langfuse.langchain import CallbackHandler`, passed in the graph config's `callbacks`.
The `langfuse` package is NOT in requirements; if the flag is on and the import fails, we
warn and continue without it. Our own JSONL trace stays the source of truth either way.

# UNVERIFIED: the CallbackHandler constructor arguments and the environment variables it
# reads (assumed LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST); check the
# Langfuse docs before enabling. Only synthetic data may ever be sent.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def langfuse_callbacks(enabled: bool) -> list[Any]:
    """Return `[CallbackHandler()]` when enabled and importable, else `[]`."""
    if not enabled:
        return []
    try:
        from langfuse.langchain import CallbackHandler  # type: ignore[import-not-found]
    except ImportError:
        log.warning(
            "LANGFUSE_ENABLED is true but the 'langfuse' package is not installed; "
            "continuing with the local JSONL trace only"
        )
        return []
    try:
        return [CallbackHandler()]
    except Exception as exc:  # misconfiguration must never break a session
        log.warning("Langfuse CallbackHandler could not start (%s); continuing without it", exc)
        return []
