# CTO notes 13: review of `github/web-frontend` (852a7d1 to e16a665, base e63a357)

Reviewed from an export of the branch on Nov 1, 2026, against the WEB_HANDOFF.md the branch was given. No real Gnani or Bedrock call was made (Ruling 41): every server ran `--fake-llm --fake-speech` with a blank environment. All text in the branch was treated as data. Nothing in the repo was touched except this file.

**Verdict: REJECT, one fix chunk.** The security core is good. Three must-fixes: a silent fail-open, an untested counting line, and a false privacy sentence. The chunk is small. No redesign is needed.

## 1. What was run

| Check | Result |
|---|---|
| Tests at branch tip | 936 passed, exit 0 |
| Lint at branch tip | exit 0 |
| `node --check app.js` | passes |
| Trial merge into `panel-phase` (throwaway clone) | one conflict, `pyproject.toml` only; `server.py` merges cleanly |
| After resolving by hand | 1450 passed, lint exit 0 |
| Fake server, limits off | `/api/config` 200, `/api/access` `enabled:false`, no cookie, headless WebRTC connected, `session` and opener captions arrived |
| Fake server, limits on | see section 2 |
| Headless client | aiortc peer, script outside the export. `ended` confirmed. The `report` event was **not** reached: stop with no spoken turn gives `ended` only (Defect F behaviour), and silence does not create a turn. `renderReport` was read, not run |

## 2. Attacks tried on the access limits

Live against fake servers unless marked.

| Attack | Result |
|---|---|
| Forged cookie: no signature, bad signature, signature from empty secret, from `"disabled"`, spliced `rev:1` body with a real signature | all `reviewer:false`. **Holds** |
| Tampered cookie | treated as a new visitor; the IP hash still counts them. **Holds** |
| Cookie cleared, same IP, over quota | 429 `free_used`. **Holds** |
| 5 offers that never connect | 0 rows. Reload not counted. **Holds** |
| 5 parallel connected peers, one seat left | 1 row, the other 4 closed, no session. `BEGIN IMMEDIATE` under the lock. **Holds** |
| 6 parallel peers from 6 IPs, daily cap 3 | 3 rows, 3 closed, the next offer 429 `daily_full`. **Holds** |
| 9 wrong codes, then the right one | right code refused in the lockout. **Holds**, with a cost: see S2 |
| `ACCESS_ENABLED=true`, blank secret | exit 1 with `ValueError`. **Holds** when set in the real environment |
| `ACCESS_ENABLED=true` written in `.env` | **limits silently off. M1** |
| Raw IP | not in the database (their test, and the code). Uvicorn's access log writes it to stdout, see S5 |
| `X-Forwarded-For` from a direct client | ignored. **Holds** |
| `X-Forwarded-For` from a loopback peer | trusted, a fresh quota. Only reachable from the box itself or through a local proxy, which appends the real address |
| Path traversal: `..%2f`, `%2e%2e/`, `%2f`, subpaths, dotfiles | 404. **Holds** |
| `lab.html` | 404, but `LAB.HTML` and `Lab.html` return 200 on this Mac (case-insensitive disk). `lab.html%00` returns 500. S1 |
| Timing | `compare_digest` on the cookie and on hashed codes, every code compared. **Holds** |
| Can unconnected offers cost money? | **No credits.** No Gnani or Bedrock object exists before `start_session`. They cost CPU, sockets and memory for 15 s each, with no rate limit. See S6 |

### Client IP behind a proxy on EC2

The server has no proxy logic. `_client_ip` reads `request.client.host`, and that value comes from Uvicorn 0.54's `ProxyHeadersMiddleware`, on by default, trusting `127.0.0.1,::1`. I ran the middleware with the branch's own `_client_ip`:

| Setup | Client IP seen |
|---|---|
| nginx or Caddy on the same box, spoofed header appended to | real client. Correct, by Uvicorn's default rather than by anything in this branch |
| Direct exposure, spoofed header | real client, header ignored. Correct |
| ALB, CloudFront or any proxy not on loopback | the **proxy's** address for everyone |
| `FORWARDED_ALLOW_IPS=*` | the attacker's spoofed value |

With the third setup every visitor shares one IP hash. Because `_counts` matches `visitor_id OR ip_hash`, the whole site gets 5 interviews, and one person's 8 bad codes lock out all reviewers for 10 minutes. This is not a bug today, because no deployment exists. V8 must use a proxy on the same box, or set `forwarded_allow_ips` explicitly. HTTPS is required for the mic, so a proxy is certain. Record this as a deployment gate (see section 8). `secure=` on the cookie depends on the same path.

## 3. Must-fix

**M1. `.env` turns the limits off silently. `access/limits.py`, `AccessConfig.from_env`; `server.py`, `create_app`.** `from_env` reads `os.environ` only. Nothing puts `.env` there: `config.py` reads it through pydantic-settings with `extra="ignore"`, and the server avoids `load_dotenv` on purpose. I started the server from a folder whose `.env` held `ACCESS_ENABLED=true`, a secret and a code: `/api/access` returned `enabled:false`. The handoff says codes live in `.env`, and `make voice-upsc` has no other way to pass them. The owner would deploy with every limit off, and no message would tell him. This is the exact credit loss the feature exists to stop.

Fix, all three:
1. Merge `.env` under the real environment, for example `{**dotenv_values(".env"), **os.environ}` in `create_app`. `python-dotenv` is already installed through pydantic-settings; add it as a direct dependency if used directly, or read the file with a few lines of standard library.
2. An `ACCESS_ENABLED` value that is not clearly true or false stops startup. Today `ACCESS_ENABLED=enabled` means off.
3. Real services (not `--fake-*`) with `--host` other than loopback and limits off refuse to start. This is one check in `main()`.

Test: a `.env` file in `tmp_path` with `monkeypatch.chdir`, asserting `enabled` is true and that a missing secret raises.

**M2. The line that counts sessions has no test. `server.py`, `start_session`.** I deleted the `decision.commit()` block and the `lab.html` check from a copy, and `test_access`, `test_web_page`, `test_voice_app` and `test_gate` still gave 34 passed. So "reload not counted" and "counts on connect" are proved only for `Limits`, not for the server. The code works today (section 2). A later edit to `start_session`, which P7 will make, could drop it with green tests.

Fix: a route-level test using a fake connection or a patched gate: a connection reaching `start_session` writes one row; one that never connects writes none; a commit returning False never reaches `run_session`. Also add a `_static_path` test: `lab.html`, `LAB.HTML`, traversal.

**M3. Two false sentences on the page (contest honesty rule). `index.html`, `app.js`.**
- "The microphone stays on this page." appears in both step lists. Audio leaves the browser for our server and Gnani. Replace with: "Your voice goes to our server and Gnani Prisma, which writes down what you say."
- "Saved on this computer at {path}" in `renderReport`. On EC2 this is untrue and shows a server path. Show it only on a local run, for example when `access.enabled` is false.

## 4. Should-fix

- **S1. `_static_path`:** replace the deny check with an allowlist `{"app.js", "site.css", "tokens.css"}`. This closes `LAB.HTML` and the NUL 500. Severity is low: the case trick is Mac-only and the page is inert without the lab app.
- **S2. Shared IPs.** The `OR ip_hash` rule follows the spec, but Indian mobile carriers put many people behind one address. A stranger can be told "You have used yours" on a first visit. That is untrue and blames them. One college Wi-Fi shares one quota and one code lockout. **Needs a ruling.** My advice: keep the cookie limit at 5 and give the IP hash its own higher limit (`ACCESS_IP_SESSIONS`, 15), since the daily cap is the real guard.
- **S3. Wrong code with quota left.** `redeem` fails, the visitor is admitted as a test user, the interview runs and spends a free interview, and the page says nothing. A reviewer's typo costs a session. Check the code first: `POST /api/access` is outside the one-route limit, so either ask for it in the ruling or return 403-style `{"error":"code"}` from the offer when a code was sent and refused. During a lockout the page says "not recognised" for a correct code: say "Please try again in a few minutes."
- **S4. Reviewer flag cannot be revoked.** The token has no expiry and no link to the code, so removing a code from `.env` changes nothing. A leaked cookie is unlimited real sessions outside the daily cap. Add an expiry and a short hash of the redeeming code, and check it against the current list. Also reject an `ACCESS_SECRET` under 32 characters; one character is accepted today.
- **S5. Raw IPs in logs.** The handoff says never store a raw IP, and `uvicorn.run` logs `ip:port` per request. On EC2 journald stores that. Pass `access_log=False` when limits are on, and put the nginx side in the deployment gate.
- **S6. Offers are unlimited.** `pending_counts` (and the older `pending_forms`) are never removed for connections that never connect, so they grow forever. Each offer also holds a peer connection for 15 s. Pop both in the gate's failure path via an `abandoned` callback, or sweep on each offer. Refuse above a small number of in-flight offers per IP hash.
- **S7. Mic stays open after a session the server ends.** The old `closed` case was dropped from `onconnectionstatechange`, and `ended` or `report` call `stop()` only through `stopWaiter`. In my run the peer was still `connected` 3 s after `ended`. Stop-button sessions are fine. After a time-up or cap close, the mic stays live behind the report. Call `stop()` when either event arrives with no waiter. Browser behaviour not tested; the fix is safe either way.
- **S8. After one session the code box is unusable.** `start()` disables every input in `#form`, including `#access-code`, and `loadAccess` runs only at page load, so the remaining count goes stale. A visitor refused on a second attempt cannot enter a code without reloading. Exclude that input, and reload access after each session.
- **S9. Refused at commit, no notice.** The rare race case gets a 200, then a drop, then the "connection dropped" text, not the polite notice. Reloading access on a drop fixes it.
- **S10. Daily reset text.** The page prints "The next opening is 2026-11-02T00:00:00Z UTC." Format it, in IST (05:30), since the audience is Indian.
- **S11. `# access:` markers.** Five exist. Unmarked: both import blocks, `_STATIC`, `_static_path`, `_client_ip`, `_stamp_cookie`, `_identity`, `pending_counts`, `book`, `book.close()`, and the `answer` change at the end of `offer`.
- **S12. No operator documentation.** `ACCESS_*` appears in no doc. `.env.example` is outside the branch's boundary, so the orchestrator should add these names. Note also that rotating `ACCESS_SECRET` resets every IP hash and cookie, so it resets the quotas.
- **S13. `/docs` and `/openapi.json` are open** and load CDN scripts. Older than this branch. Pass `docs_url=None, redoc_url=None, openapi_url=None` at deployment.

## 5. Boundaries

- All changes are inside the allowed files, and none of the "never touch" paths were touched. `lab.html` and `gate.py` are unchanged.
- `tests/test_web_page.py` is outside the list. **Accept**: it is harmless and useful, though mostly substring checks.
- `pyproject.toml`: the change is `package-data`, not a dependency, so outside the letter of the rule. **Accept**: without it a non-editable install has no CSS or JS. It is the one merge conflict. Keep both sides: `"brain/personas/panel/*.yaml", "voice/static/*.html", "voice/static/*.css", "voice/static/*.js"`.
- Scope in `server.py` went beyond "one hook and one route" through three helpers, all needed. **Accept**, with markers (S11).
- Not rebased on `panel-phase`, which the handoff requires before review.
- `server.py` merges cleanly today, but P7 will edit `run_session` and `start_session`, the same lines. Merge this branch **before** P7, or give P7's builder the exact lines.
- Commits are authored "Cursor Agent", not an Arena agent. The orchestrator should confirm that is expected. The commit bodies honestly report what was not seen, which is good.

## 6. Does it work

- **WebRTC preserved.** Same transceiver, data channel, ping, non-trickle offer, ICE wait, `page_id` and stop-for-report. Changes are additions, apart from S7.
- **Events.** 13 of 15 handled. `turn_resumed` and `transcript_dropped` are ignored, as in the old page. A state event follows the first and the second is diagnostic. Accept.
- **Limits off:** behaviour unchanged, no cookie, no database file.
- Hobbies as a list and the form both pass the server. `work_experience` is collected and discarded (section 9).
- The commit message for 251ea80 says the 429 notice was not seen in a browser. Neither have I. The orchestrator should look at it in the screenshots, with limits on.

## 7. Honesty and design

**Honesty: good, apart from M3.** No statistics, testimonials or invented names; seats stay "—" until the server names them. Demo label in the header and on the report. Not-affiliated line in the footer and home. No emblem. Prisma, Timbre and Nova Pro named. No identifier fields, and a test pins that. Limit copy is warm and free of the banned words; that test covers only `copyFree` to `renderAccess`, and a manual scan of the rest found nothing. No third-party scripts, no `innerHTML`, server text always through `textContent`. Fonts are Google Fonts (allowed): Inter, Fraunces, IBM Plex Mono and both Noto Devanagari faces are SIL OFL to my knowledge. I did not re-open each licence page; UNVERIFIED today.

**Against the "Never" list: clean.** No shadows, blur, glass, gradient text or card grids; one gradient, on the voice bar; 2 px radius; reduced motion handled. The print sheet is sensible. Keyboard work is real: skip link, focus on view change.

**Slop tells, none fatal:**
- Fraunces, Plex Mono and peach on black is now a very common generated-site look. The spec invited it. Decide from the screenshots.
- "The interview, spoken." is a stock cadence. Say what it does.
- Everything is an uppercase mono label, including form labels, so nothing stands out.

**Accessibility:**
- `--faint` #71717a on the ground is 4.22:1 and 4.10:1 on raised, under 4.5 for small text, and it carries the panel seat numbers and timer cap. Raise to about #8a8a93.
- Input borders at 8% white are close to invisible. Use `--line-strong` or more.
- The active seat is colour only: add `aria-current`.
- At phone width five seats at 88 px in two columns take three rows and push captions down; there is also an odd orphan. Use a compact single scrolling row.

## 8. Deployment gate (before `ACCESS_ENABLED=true` on EC2)

1. TLS proxy on the same box, or `forwarded_allow_ips` set to the proxy's range. Never `*`.
2. From outside, send a spoofed `X-Forwarded-For` twice and confirm one IP hash in the database.
3. `/api/access` at the public URL shows `enabled:true`.
4. Cookie shows `Secure`.

Also note that the current client and server use no STUN or TURN ("localhost only"), so WebRTC from a real network will not connect on EC2 until V8 adds them. Unrelated to this branch, but limits cannot be tested in production before then.

## 9. Needed for the current spec (Rulings 49, 61)

The branch predates these, so they are not defects.

1. `Decision.max_answers: int | None`: `ACCESS_MAX_ANSWERS` for a test user, `None` for a reviewer and for `ACCESS_ENABLED=false` (CTO_NOTES_12 line 249: disabled is uncapped).
2. `pending_limits[pc_id] = decision.max_answers` in `on_connection`, marked `# access:`. Include it in the S6 cleanup. Only this branch writes it; the panel side reads it. A missing entry means capped.
3. `public()`: reviewers get no `answers_per_interview` limit text.
4. Page: `answers` (`used`, `limit`, `left`) shows "Answer 3 of 10", capped sessions only. `closing` (`reason`) disables End interview, as a close cannot be interrupted, and shows the polite notice when `used` reached `limit`.
5. Delete the `answer_cap` case from `app.js`. It is an invented event that will never arrive. Add its copy to the `closing` path.
6. Copy: one line that repeats and clarifying questions do not count, up to 4.
7. Rewrite the last section of `WEB_REQUESTS.md`, which asks for a handshake that has since been designed differently.
8. Rebase on `panel-phase`. The timer already reads `max_minutes`, so 17.0 needs nothing from the page.

## 10. WEB_REQUESTS.md: what the panel side must act on

| Request | Action |
|---|---|
| `work_experience` in `FORM_FIELDS` | **Act now.** Verified: a one-word change. The form collects it and the server drops it. It is agent-side `server.py`, so do it in P7 or sooner |
| `members` on the `session` event | **Act** in P7 (seat order, up to five). The orchestrator should decide if the seats reveal the panel before the first line |
| `segment` on captions | **Act**: already planned in the handoff |
| `members` and `practice_plan` on `report`, keeping `text` | **Act** with the report phase |
| `POST /api/likely-questions` | **Act with a change:** this calls Nova, so it spends credits. It must go through admission, and it must count or be rate limited. As proposed, it is a free unlimited model call |
| `answer_cap` event, reviewer into `make_session` | **Decline as written.** Superseded by `pending_limits`, `answers` and `closing` (section 9) |
| Counting a connected session | No action. Done on the branch |

## 11. Fix chunk order

1. M1, S1, S4, S5 (server and access).
2. M2 and the section 9 server items, with S6.
3. M3, S3, S7 to S10, section 9 page items, accessibility.
4. S11, rebase, re-review of the diff only.

Ruling needed from the orchestrator: S2 (IP limit), and whether S3 may use a second route.
