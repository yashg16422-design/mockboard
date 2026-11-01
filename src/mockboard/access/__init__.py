"""Spoken-interview limits. No FastAPI, no raw IP addresses.

`Limits.admit(identity, now)` decides. `Decision.commit()` is what counts a
session, and the voice app calls it only after the peer connection is up.
A page reload that never connects does not commit, so it is not counted.

The answer cap is not enforced here. `Decision.max_answers` goes into the server's
`pending_limits`, and the board closes the interview itself (Rulings 49, 61).
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
