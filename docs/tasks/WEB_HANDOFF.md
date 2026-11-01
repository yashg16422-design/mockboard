# Web handoff: the Mock Board site and access limits

Work split agreed by the owner on Nov 1, 2026. Claude Code builds the agents (the Panel phase, branch `panel-phase`). A second agent on arena.ai builds the website and the access limits on branch `web-frontend`. The owner reviews both, and they merge into `main`.

## Paste this into Arena Agent Mode

> You are the frontend engineer and product designer on Mock Board, a voice-first Hinglish mock of the UPSC Personality Test, built for Gnani.ai's Great Indian AI Internship Challenge (submission Nov 9, 2026).
>
> Start from branch `panel-phase` and create branch `web-frontend`. Read `docs/tasks/WEB_HANDOFF.md` from "Boundaries" onward and follow it exactly: it holds the file boundaries, API contract, design direction and access-limit spec. Read `CLAUDE.md` for project background, but do not make real-service checks: you have no credentials and must not ask for any.
>
> Deliver, in this order, in small commits: (1) the site shell and design tokens, (2) the home page, (3) the form and interview room wired to the existing WebRTC flow, (4) the report view, (5) access limits. Run only free targets: `make test`, `make lint`, `make voice-fake`. If something you need is missing from the API, do not change the agent code: list it in `docs/tasks/WEB_REQUESTS.md`. Finish each commit with what you verified and how. Do not claim anything works unless you ran it. Do not merge into `main`.

## Boundaries

Another agent is changing the backend at the same time, so these limits are what keep the merge clean.

**You own**
- `src/mockboard/voice/static/index.html`, plus any new files in `src/mockboard/voice/static/`, except `lab.html`.
- `src/mockboard/access/` (new package) and `tests/test_access.py`.
- `docs/tasks/WEB_REQUESTS.md`.

**You may touch, minimally**
- `src/mockboard/voice/server.py`: only the `/api/offer` admission hook, one `/api/access` route and static-file mounting. Mark each with `# access:`.
- `pyproject.toml`: only if access needs a dependency. Prefer the standard library, including `sqlite3`.

**Never touch**
`src/mockboard/brain/`, `modes/`, `session.py`, `prompts/`, `speech/`, `voice/brain_processor.py`, `voice/pipeline.py`, `config.py`, `docs/CTO_NOTES_*`, `docs/ORCHESTRATOR_LOG.md`, `.env`, `tests/test_*invariants*`.

**Stack:** plain HTML, CSS and JavaScript, no build step, served by the existing FastAPI app on the same origin as `/api/offer`. Deployment is one EC2 box. Self-host fonts in `static/`, or load them from Google Fonts. Third-party scripts are not allowed.

**Run it (free, no keys):** `make venv`, `make voice-install`, `make voice-fake`, then open http://localhost:7860. `make voice-fake` runs a fake model and fake speech. Never run `make voice-upsc`, `voice-debate`, `voice-record`, `voice-lab`, `stt-ab` or `cli-real`, or anything in `probes/`.

## API contract today

- `GET /api/config`: `mode`, `fake_llm`, `fake_speech`, `voice`, `tts_model`, `stt_model`, `llm`, `region`, `max_minutes`, `record_audio`.
- `POST /api/offer`: body has `sdp`, `type`, `pc_id`, `restart_pc`, and `request_data: { form, page_id }`. A bad form returns 400 `{ "error": "form", "problems": [...] }`.
- `PATCH /api/offer`: ICE candidates.
- Form fields, from `CandidateForm`: `home_state` (required, 2-60 chars), `district`, `degree_subject` (required, 2-80), `hobbies` (up to 3), `optional_subject`, `work_experience` (up to 200), `interview_language` (`hinglish`, `hindi` or `english`). **There is deliberately no name, roll number, phone, email or college field. Never add one.**
- Server events arrive on the WebRTC data channel `messages` as `{ "type": "mockboard", "event": ..., "t": ..., ...fields }`. Events: `session`, `state`, `caption`, `line_end`, `turn_resumed`, `turn_reopened`, `transcript_dropped`, `no_speech`, `time_up`, `error`, `tts_failed`, `stopping`, `ended`, `preparing_report`, `report`.
- The working client is the current `index.html`. Keep its WebRTC logic and restyle around it. Read `voice/brain_processor.py` to see each event's fields, but do not edit it.

**Coming from the Panel phase (do not depend on these until they land on `panel-phase`):** captions gain the panel member and agenda segment; five members replace three; the report is grouped by member with the weakest question, an outline and a practice plan; a "Likely questions" utility appears. Design the room for five named members and the report for that structure now, and show only what the server really sends.

## Pages

1. **Home:** what this is, one honest explanation of how it works, start button.
2. **Form:** the fields above, one calm column. Include "Likely questions" as a disabled slot until the API exists.
3. **Interview room:** the core of the product. The panel of five as a quiet row, with the current speaker clear at a glance. Live captions from the board and the candidate, a timer, mic state, and end interview. Include mic-permission, connecting, no-speech, reconnect and error states.
4. **Report:** printable, grouped by member once the data exists; plain text from the `report` event until then.

Everything must work at phone width and with a keyboard, and respect `prefers-reduced-motion`. Keep it responsive while the voice call runs: the audio path matters more than decoration.

## Design direction

The aim is a serious examination room rendered as precise engineering documentation. Nothing should look like an AI landing page. "Inspired by" means the qualities below: never copy logos, copy, images, or layouts one to one, and never use Gnani's or DAQ's branding as our own.

**What the two references really do (read from their CSS on Nov 1, 2026)**
- **DAQ:** near-black neutrals (`#050505`, `#0A0A0A`, `#141414`), text in `#e4e4e7` and `#a1a1aa`, hairlines at `rgba(255,255,255,.08-.1)`, Inter with monospace labels. Numbered sections ("01 / Paradigm Shift"), labelled diagrams, hover-to-inspect, dense precise copy.
- **Gnani:** Inter for body, Gambarino as a serif display face, quiet greys (`#f9fafb`, `#f2f4f7`, `#e4e7ec`, `#636363`), a purple accent (`#9f73f4`), peach and orange gradient artwork, large numeric proof, generous space.

**Our system**
- Dark, near-black ground from the DAQ neutrals, hairline borders, almost no fills.
- Inter for text; one high-contrast serif for large display lines only; monospace for labels, timers and section numbers such as `01 / The Panel`. Check the licence of any font before using it.
- One warm accent, sparingly, for the active speaker and the primary action only. One soft gradient at most, drawn from the peach-orange warmth, tied to the voice (for example the speaking state).
- A real type scale, an 8 px spacing grid, sharp or barely rounded corners.
- Motion is rare, short and meaningful: speaker change, caption arrival. The voice-activity indicator is the one expressive element.
- Hindi must look right: pick a Devanagari face that sits well beside Inter, since captions may use both scripts.

**Never**
- Purple-to-blue gradients, glassmorphism, glowing orbs, floating blobs, emoji icons, generic icon-card grids, three identical feature cards, gradient text, big drop shadows, or bounce animation.
- "Unlock", "Revolutionize", "Seamless", "Elevate", "Your AI-powered" or similar copy.
- Stock photos, or AI-generated faces.

**Honesty (contest rule, not styling)**
- Never invent statistics, testimonials, users, partner logos, success rates or board names. If a section needs one of those, drop the section.
- Name the real models where relevant: Gnani Prisma for speech to text, Gnani Timbre for text to speech, Amazon Nova Pro for the board.
- Say plainly that this is a practice tool, not affiliated with UPSC. Do not use UPSC's emblem.
- When `fake_llm` or `fake_speech` is true, show a visible "demo mode" label.
- Sample data in any screen must be clearly synthetic.

## Access limits (server-side)

Real sessions cost the owner credits, so every limit is enforced by the server before a session is built. Page checks are only for messaging.

**Rules**
- A visitor gets `ACCESS_FREE_SESSIONS` interviews, default **5**.
- Reviewers enter an access code and are not limited by that count.
- Each interview is capped at `ACCESS_MAX_ANSWERS` candidate answers, default **20**, on the server.
- `ACCESS_DAILY_SESSIONS` caps all non-reviewer sessions per UTC day, default **30**. **This is the real credit guard**, because per-visitor limits can be dodged by clearing cookies.
- `ACCESS_ENABLED=false` (default) keeps local development and `voice-fake` unchanged.

**How**
- `src/mockboard/access/` is a pure module with no FastAPI import, with one function `admit(identity, now) -> Decision`, backed by a `sqlite3` file under a git-ignored `runs/` path.
- Identity is a signed, HttpOnly, SameSite=Lax cookie holding a random id, plus a salted hash of the client IP so clearing cookies alone does not reset the count. Never store a raw IP. Sign with `ACCESS_SECRET` from the environment.
- Reviewer codes come from `ACCESS_REVIEWER_CODES` in `.env`, compared in constant time, stored only as a signed cookie flag, never in the repo. Failed code attempts are rate limited.
- Hook into `POST /api/offer` before any WebRTC work. Refuse with 429 `{ "error": "limit", "reason": "free_used" | "daily_full", "used": n, "limit": n }`, and show a calm message on the page.
- `GET /api/access` returns remaining interviews and reviewer status for the page.
- **Counting:** a session counts only when its WebRTC connection really connects. Page reloads must not count. The connection point is the `gate.offer` callback in `/api/offer`. If counting there needs a change outside your files, write it in `WEB_REQUESTS.md`.
- **The answer cap needs the agent side.** Do not implement it in the brain. Add it to `WEB_REQUESTS.md`, and Claude Code will wire it into the panel.
- Tests go in `tests/test_access.py`: quota reached, reviewer bypass, daily cap, tampered cookie, disabled mode, no raw IP stored, reload not counted.

## Merge plan

- `web-frontend` rebases on `panel-phase` regularly, and always before review.
- After the Panel cutover (P7), the UPSC session is live, and the page is checked against it with `make voice-fake`.
- Claude Code reviews `web-frontend` (`make test`, `make lint`, diff against the boundaries) before anything reaches `main`.
