"""Spoken-interview limits. No FastAPI, no raw IP addresses.

`Limits.admit(identity, now)` decides. `Decision.commit()` is what counts a
session, and the voice app calls it only after the peer connection is up.
A page reload that never connects does not commit, so it is not counted.

The answer cap (how many candidate messages one interview may contain) is not
enforced here. The board has to close the interview itself. See
`docs/tasks/WEB_REQUESTS.md`.
"""

from mockboard.access.limits import (
    COOKIE,
    AccessConfig,
    Decision,
    Identity,
    Limits,
    hash_ip,
    new_visitor_id,
    read_token,
    sign_token,
)

__all__ = [
    "COOKIE",
    "AccessConfig",
    "Decision",
    "Identity",
    "Limits",
    "hash_ip",
    "new_visitor_id",
    "read_token",
    "sign_token",
]
