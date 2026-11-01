# CTO_NOTES_16: review of Milestone B (P5+P6) and Milestone C (P7 cutover)

Range: 2b94042..a3811e3. Verdict: **ACCEPT**, no must-fixes. Nothing here was run on real Nova, Prisma or Timbre.

## Verified by running

1. **Debate unchanged.** Checked out 65b8a88 in a temp worktree and dumped every model request of the
   three locked debate scripts from both trees. Stdout is byte-identical in all three. The listener,
   actor and judge requests are identical in every field (system, user, token cap, tool, temperature).
   Only the fact-challenge script's one `librarian` call differs, and only inside the system text's
   "## The interview board (persona pack)" block. With that block masked, old and new both hash to
   85fa5b76..., which is `DEBATE_FACT_REQUESTS_MASKED_SHA256`. The old raw hash was 191eb84a... (the old
   lock) and the new raw hash is 5a519b35.... The board md5 is f2a23f33... for the debate script. The
   deviation is legitimate and hides no debate change.
   Latent debate issue, older than P7 and not P7's fault: debate's fact-check prompt is built from the
   default (UPSC) kit, so a debate librarian reads the UPSC interview persona pack.
2. **Scripted fake sessions** (`fake_panel.txt`, Hindi form, my own edge scripts) read line by line.
   Ritual, five members in order, a hand-off at each change, "Namaste" only at the first hand-off,
   variants that never repeat back to back, one close. Repeat at the first question is free and re-asks
   the real question. Silence is free and gets a reassure. Devanagari "tourism ke alawa..." is caught,
   acknowledged, handed off, and the term never returns. Cap 5 reaches a proper close at the 5th answer.
   `--max-turns` under UPSC and `--max-answers 4` are refused.
3. **Targeted pytest:** `test_cutover`, `voice/test_panel_voice`, `voice/test_access_routes`,
   `test_modes`: 86 passed.

## Should-fix (none blocks)

- S1 `agenda_plan.planned_seed_violations` accepts "Kanpur ke chamda udyog ne yahan ki zindagi kaise
  badli?" (probed). That is a place fact presupposed inside a question, and the module's own
  UNVERIFIED comment admits it. Cheapest net: reject a planned seed that names the district or state
  (the template lines already carry the place name). Do it before P12 reads real plans.
- S2 Rephrased requests the frozen detector misses and the fake listener cannot vote on ("sorry, kya aap
  dobara pooch sakte hain" got a pressure move; "aap kuch aur puchiye, kanpur ke baare mein nahi" and the
  Hindi "kya aap sawal dobara pooch sakte hain" were treated as answers). This is the known 0.75 recall
  (Ruling 57). P12's real transcripts are the only fix. Say so in the demo.
- S3 `voice/server.py --max-turns` is accepted for UPSC and silently ignored (the panel sets its own
  cap), and `docs/VOICE_QUICKSTART.md` line 73 still advertises `--max-turns 4` "for either mode". Refuse
  it for UPSC as the CLI does, or fix the doc.
- S4 The `reassure` template after silence says "is vishay se aapka pehla vaasta kab pada?" with no
  referent when the question was about motivation. Harmless, but vary it with the segment.
- S5 Site: `index.html` loads Google Fonts from the CDN, which sends every visitor's IP to Google and
  contradicts the page's own privacy statement. Self-host the fonts before deploy. Also still open from
  CTO_NOTES_13 section 8 for Milestone F: cookie `Secure` depends on `request.url.scheme` behind a
  proxy, and `forwarded_allow_ips` must be set narrowly.

## Checked and fine

`Settings.topics` and `max_turns_per_topic` are gone (extra="ignore" makes a stale `TOPICS=` in `.env`
harmless). One persona table: `personas/*.yaml`, `personas/panel/` deleted, names appear in no code.
Wrap-up 960 s and hard stop 1020 s are validated (wrap-up below hard stop). Debate keeps 600 s.
`pending_limits.get(pc_id, book.config.max_answers)` fails closed, and a test pins it. `answers`,
`closing`, `members` and caption `segment` are emitted. `run_session` closes the session in a `finally`
(finished, raised, cancelled, failing cleanup are all tested), `BrainProcessor` closes on End/Cancel,
and `aclose` is idempotent. Prisma rotation is reachable through `from_settings`
(12 min quiet, 14 min hard, below 15 min). Access: HMAC cookie, a reviewer code that is revocable and
rate limited, IP hash only, no raw IP in logs, uvicorn access log off when limits are on, static
allowlist, `/docs` off, no secret in `/api/config`. Limit copy never says error, denied or blocked.
Milestone B: `requests.py` and `LISTENER_TOOL` are untouched, no old prompt file was edited (only
`actor.v5`, `agenda_plan.v1` and `listener.v4` were added, and the CHANGELOG logs them). The veto is
exactly the Ruling 53/56 table: only weak change patterns, only a healthy listener's "none", only on
turns over 8 words, and a failed listener never vetoes. `end` needs a rule hit. The plan is strict
(character set, one question, 6 to 28 words, no digit, quote or Devanagari, no title with a name, no
capitalised word the form does not hold, nothing refused), and a failed seed falls back alone to its
template. A refusal now bans "Kanpur" for "Kanpur Nagar", but "Uttar Pradesh" no longer bans "uttar".

## Not verified

Rendering in a browser. Nova behaviour of `listener.v4`, `actor.v5` and `agenda_plan.v1`. The 1.5 s plan
wait. Prisma rotation against a live server. Python 3.12.
