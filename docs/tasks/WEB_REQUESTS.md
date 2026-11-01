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

Designed in `docs/CTO_NOTES_12.md` section 9 and superseding the earlier `answer_cap` request. `/api/offer` writes `pending_limits[pc_id] = decision.max_answers`; the panel reads it at P7. A missing entry means capped. The page uses the `answers` and `closing` events. Uncapped sessions get neither limit text nor `answers`.
