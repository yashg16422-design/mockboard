# Web fix 01: changes needed on `web-frontend`

The review is in `docs/CTO_NOTES_13.md`, on `panel-phase`. The design and structure stay as they are: this is a fix chunk, not a redesign. `docs/tasks/WEB_HANDOFF.md` still applies. Rebase `web-frontend` on the latest `panel-phase` first. `pyproject.toml` has one conflict: keep both sets of package-data entries.

Work in this order, one commit per group, and report what you ran and its result. Item numbers match the review.

## Must-fix

- **M1. Limits must not silently stay off.** `AccessConfig.from_env` reads only `os.environ`, so `ACCESS_ENABLED=true` in `.env` leaves limits off. Merge `.env` values. Refuse any unrecognised `ACCESS_ENABLED` value with a startup error. Refuse to start with real services on a non-loopback host when limits are off.
- **M2. Route-level tests.** Removing `decision.commit()` and the lab check passed every test. Add tests through the `/api/offer` and static routes, so that both of those mutations fail. Show that they fail.
- **M3. True copy.** Replace "The microphone stays on this page": audio goes to our server and to Gnani, and the page should say so. Replace "Saved on this computer at {path}", which is untrue on a server.

## Should-fix

- **S1.** `_static_path` uses an allowlist of `app.js`, `site.css` and `tokens.css`.
- **S2.** Keep the cookie limit at 5. The IP hash gets its own higher limit, `ACCESS_IP_SESSIONS`, default 15, because many people in India share one address. A first-time visitor must never be told they have used their interviews.
- **S3.** A second route is allowed: `POST /api/access` checks a code before the interview. A wrong code must never spend an interview. During a lockout, say "Please try again in a few minutes."
- **S4.** Reviewer tokens have an expiry and a short hash of the redeeming code, checked against the current list, so removing a code revokes it. Reject an `ACCESS_SECRET` under 32 characters.
- **S5.** `access_log=False` when limits are on, so no raw IP reaches the logs.
- **S6.** Remove `pending_counts`, `pending_forms` and `pending_limits` entries for connections that never connect. Refuse above a small number of in-flight offers per IP hash.
- **S7.** Stop the microphone when `ended` or `report` arrives, including sessions the server ends.
- **S8.** Keep the access-code input usable after a session, and reload access after each session.
- **S9.** Reload access on a dropped connection, so the polite notice appears.
- **S10.** Show the daily reset as a readable time in IST.
- **S11.** Mark every `server.py` change with `# access:`.
- **S13.** Pass `docs_url=None, redoc_url=None, openapi_url=None`.
- Contrast and phone-seat fixes from section 7 of the review.

## Current spec (Rulings 49, 61)

1. `Decision.max_answers: int | None`: `ACCESS_MAX_ANSWERS` for a test user, `None` for a reviewer and when `ACCESS_ENABLED=false`.
2. `pending_limits[pc_id] = decision.max_answers` in `on_connection`, marked `# access:`. Only this branch writes it. A missing entry means capped.
3. `public()`: reviewers get no answer-limit text.
4. Page: `answers` (`used`, `limit`, `left`) shows "Answer 3 of 10", capped sessions only. `closing` (`reason`) disables End interview and shows the polite notice when `used` reached `limit`.
5. Delete the `answer_cap` case from `app.js`. That event will never arrive. Move its copy to the `closing` path.
6. Add one line of copy: repeats and clarifying questions do not count, up to 4.
7. Rewrite the last section of `WEB_REQUESTS.md` to match this.

## Do not do

- Do not implement `POST /api/likely-questions`: the panel side builds it, through admission.
- Do not change files outside the handoff's boundaries. `tests/test_web_page.py` is accepted. `.env.example` is handled by Claude Code.
