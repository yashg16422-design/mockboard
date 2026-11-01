# CTO_NOTES_17: review of Milestone D (P8–P11)

Range: `10cf037`, `c0aa1a7`, `fbe2da7` on `panel-phase` (parent `a3811e3`, Ruling 76).
Verdict: **ACCEPT**, no must-fixes. Nothing here was run on real Nova, Prisma or Timbre.

## Verified by running

1. **Targeted pytest** (no full `make test`; orchestrator already had 2707 passed):
   `test_likely_questions`, `test_report_panel`, `test_check_trace`, `test_agenda_plan`,
   `test_modes`, the three `/api/likely-questions` route tests, the utility-count test,
   `--max-turns` refusal, and `test_barge_in`: **165 passed**.
2. **Debate hashes do not need to change.** `tests/test_modes.py` in this range only moved
   the UPSC board-line md5 (`e748940f…` → `e32140d5f91a41cfa21da8b5860720fa`) and the
   comment that explains it. `DEBATE_BOARD_LINES_MD5` is still `f2a23f33…`. Prefix, tools,
   prompts, templates, and the three request locks are the same bytes as at P7. Debate
   golden file is untouched.
3. **`make likely-questions` / CLI `--template-only`** on `examples/forms/sample_form.json`
   (Kanpur Nagar / Electronics): 14 questions, five members, English structure-only hints,
   the honest note, “no model was called”. The questions name the district because they
   are the **template** agenda (that is the job of the templates). Hints do not. No TEST
   CARD text, no figure, no invented source.
4. **Golden transcript** (`tests/golden/upsc_cli_fake.txt`): Chairperson opener is
   “Electronics padhne ke baad…”, Smt. Menon is “Apne zile mein rehkar…”, silence
   reassure is “is mudde par”. The file grew by the by-member report, weakest outline,
   practice plan and “Replay this moment (text):” only. Board-line md5 stays `e32140d5…`.
   The Exampleland / SYNTHETIC TEST CARD block is the `--fake` fact-check path (Ruling 36);
   it is not a leak.
5. **Working tree** after `fbe2da7` is clean except ignored `.env`, caches, `.venv`,
   `traces/`. No `.cto_wt*` present.

## Should-fix (none blocks)

- S1 `eval/panel/check_trace.py` fails a honest short sitting that never left `form_hr`
  (`handoffs < 1`). An `end` on the first segment, or a one-question replay trace, is a
  valid panel session. Soften: require a hand-off only when more than one segment appears.
- S2 `turn.question` for `weakest` is the whole board line, so the golden’s practice
  prompt is “Samajh mein aata hai. Agar yeh tareeka na chale…”. Store the seed, or strip
  the acknowledgement, before P12’s real report is shown to a candidate.
- S3 P10’s `judge.v2` / `weakest_outline` was not built. `decorate_report` always uses
  `DEFAULT_OUTLINE`. That is the honest cut (code, not a model) and the hook is there.
  Do not add a model outline until a real judge run exists.
- S4 The checker’s refused-term scan is `token in blob.split()`. A two-word avoid term
  (“Kanpur Nagar”) never matches as one token. Use the same expander as `avoid_terms`,
  or keep requiring the bare alias. Not a leak today because the director stores the
  bare name.
- S5 Ruling 76 item 5 (self-host fonts, cookie `Secure` behind a proxy) stays a
  Milestone F gate. Not D.

## Checked and fine

**Ruling 76.** (1) `planned_seed_violations` refuses district/state (whole value or a
long non-generic word: “Kanpur” for “Kanpur Nagar”, never “uttar” alone). `agenda_plan.v2`
is a new file; v1 is untouched; CHANGELOG has the row. Fake planner no longer names the
place. (3) `voice/server.py` refuses `--max-turns` for UPSC, keeps it for debate;
`VOICE_QUICKSTART.md` says “debate only”. (4) Fake reassure is `{topic}` (“apne zile”,
“apni padhai”, “is mudde”), never a bare “is vishay”.

**P8.** `TTSUpdateSettingsFrame` alone would not have changed Timbre: `request_body`
sends `self.voice` and the plugin refuses every v3.0 id. `_update_settings` takes the
voice out of the delta, `check_voice`s it, sets `self.voice`, and a refused id is
logged (`voice_refused`) and ignored. Each send captures `_request_voice` so the
first-byte resend cannot pick up a later switch. The voice loop names a voice at the
start of every line (`_reset_ledger` clears `_line_voice`) and on a change inside a
line; held `line` / `first_rest` units flush at a voice change. Empty table / `--voice`
/ debate send no frame. `check_panel_voices` stops a bad table at start.
`DEFAULT_PANEL_VOICES` uses only lab catalog ids; v2.5 repeats (three voices, five
seats) but never between the Chairperson and a neighbour. Fake TTS records
`(voice, text)` and beeps at a pitch per voice.

**P9.** Template path is free and makes no call. Nova path is one `summary.v1` /
`record_hints` call; each hint is validated alone (`hint_violations`: one sentence,
6–30 words, no digit/quote/place/source/instruction, no district/state). A bad hint
falls back alone. `/api/likely-questions` goes through `admit_utility`, **counts
before the model is called**, and a full quota still returns the list with standard
hints and a polite `limit` note that never says error, denied or blocked. Reviewers
are free. Debate is 404. Page button sends `{form: …}`. `build_deps` still strips
synthetic cards unless the LLM is fake (Ruling 36).

**P10.** `by_member`, `weakest` and `practice_plan` are built by code. Garbled,
silent, fallback and request turns are never ranked. Practice plan skips
`insufficient_evidence`. Replay is text-only behind `ReplaySource` / `TextReplay`
(Ruling 48): CLI `--replay <report.json>` sits the flagged question once, budget 1,
`skip_judge`, compares word count and balance. The page “Practise this moment”
shows the question and outline and tells the candidate to speak aloud. No audio
cache. Debate is refused.

**P11.** Checker: session_start, every turn has segment and member, a refused term
does not return, counts stay inside the cap, close or report present; a debate
trace is skipped (exit 0). `make check-trace TRACE=…` exists. Docs/CLAUDE.md/
WALKTHROUGH/ADR-010 updated. Old prompt files were not edited.

**Hard constraints.** `brain/requests.py` and `LISTENER_TOOL` are untouched.
`JUDGE_TOOL` / `DEBATE_JUDGE_TOOL` are separate; `HINTS_TOOL` is new. Wrap-up
960 s, hard stop 1020 s, grace 20 s. From `closing`, `board_phase()` is `LOCKED`
(a cough cannot start a turn); debate’s verdict is not locked. No real Bedrock or
Gnani call in this milestone (Ruling 41).

## Not verified

`summary.v1` and `agenda_plan.v2` on Nova. That a **real** Timbre socket honours a
different `voice` per request without a reconnect (P8; Milestone E item 1). Prisma
rotation against a live server. Python 3.12. Browser rendering of the likely-questions
list and the “Practise this moment” box (the JS was read; it was not clicked).
`# UNVERIFIED:` the placeholder voices suit their members, and Poorvi on a
male-titled seat.
