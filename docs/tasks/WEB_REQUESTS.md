# Requests from the web branch

The page does not change the brain, the session, or the prompts. These are gaps between this page and the API on `panel-phase`. Each one is something the page already has a place for, and will show only when the server sends it.

## `work_experience` is dropped

`CandidateForm` accepts `work_experience` (up to 200 characters). `FORM_FIELDS` in `src/mockboard/voice/server.py` does not list it, so `parse_form` throws the field away before the session is built. The form sends it. Please add `"work_experience"` to `FORM_FIELDS`.

## Name the five seats up front

The room has five seats. A seat gets a name when a board caption arrives with `speaker`, and only then. The `session` event is `{ "mode", "session_id" }`. Please add `members`: an array of up to five display names, in seat order, so the row is labelled before anyone speaks. The page will not invent names.

## Agenda segment on a caption

The page prints `segment` beside the speaker when a caption has that field. Captions today are `side`, `speaker`, `move`, `text`, `kind`, and on the candidate side `late` and `turn`. `segment` is not sent yet.

## Likely questions

The form has a disabled slot labelled "Likely questions". There is no route for it. Please add one when the utility exists. A shape the page can use:

`POST /api/likely-questions` with the same `form` object as the offer.

`{ "questions": [ { "member": "…", "text": "…" } ] }`

The page will not write questions of its own.

## Report grouped by member

The `report` event is `text`, `path`, and `trace`. The page shows `text`. When these optional fields are also present it will group the page by member instead of the plain text:

- `members`: `[ { "name", "notes", "weakest_question", "outline" } ]`
- `practice_plan`: `[ "…" ]`

Leave `text` in place either way, so a page that does not understand the new fields still has the report.

## Counting a connected session

Done on this branch, inside `src/mockboard/voice/server.py`. `POST /api/offer` only checks the quota. `Decision.commit()` runs in `start_session`, which `PageSessions` calls after the peer connection is actually up. `gate.py` did not need a change. A reload that never connects is not counted. Covered by `tests/test_access.py`.

The reviewer flag is on the signed cookie and in `GET /api/access`. It is not passed into `make_session`. That part is still open, below.

## The answer cap belongs to the Chairperson

This branch does not count candidate messages and does not close the interview. Real sessions need both, on the agent side, so a line is never cut off.

When `create_app` builds the session, please pass:

- whether this visitor is a reviewer (the access cookie on this branch; see `src/mockboard/access/`)
- `max_answers`: `ACCESS_MAX_ANSWERS` for a test visitor, and no cap for a reviewer

The Chairperson should close the interview at the cap, between lines. Please emit a data-channel event before that closing line, for example:

`{ "type": "mockboard", "event": "answer_cap", "used": 10, "limit": 10 }`

The page will explain it in the same plain words as the other limit notices. It will not invent a count the server did not send.
