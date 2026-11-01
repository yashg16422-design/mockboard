"""Spoken-interview limits. No FastAPI, no raw IP addresses.

`Limits.admit(identity, now)` decides. `Decision.commit()` is what counts a
session, and the voice app calls it only after the peer connection is up.
A page reload that never connects does not commit, so it is not counted.

`Limits.admit_utility(identity, now, kind)` is the same for a call that spends Nova credits outside
an interview (P9, "likely questions"): its own per-cookie, per-IP and daily counts, counted by
`UtilityDecision.commit()` before the model is called, reviewers and disabled mode exempt.

The answer cap is not enforced here. `Decision.max_answers` goes into the server's
`pending_limits`, and the board closes the interview itself (Rulings 49, 61).
"""

from mockboard.access.limits import (
    COOKIE,
    AccessConfig,
    Decision,
    Identity,
    Limits,
    UtilityDecision,
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
    "UtilityDecision",
    "hash_ip",
    "new_visitor_id",
    "read_token",
    "sign_token",
]
