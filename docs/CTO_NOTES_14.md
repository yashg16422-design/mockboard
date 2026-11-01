# CTO Notes 14: P2.1 Review (Nov 1, 2026)

**Scope.** Commits effefa9, bc800c1, cadca61, 9a09569 on `panel-phase`, base b3e9c4a, against CTO_NOTES_12 and Rulings 59 to 62. No source edited, nothing committed, nothing pushed. **No Gnani or Bedrock call was made (Ruling 41).** Every trial ran on throwaway exports or throwaway repositories in the scratchpad. P2.1 touches no external API, so no primary docs were needed. Timbre's real pronunciation is `# UNVERIFIED:` throughout.

## 0. Verdict

**ACCEPT.** There are no must-fixes. M1, M2, M3 and S1 to S10 are all done, and all five surviving P2 mutations are now caught. The hook is sound for ordinary commits, with known gaps listed in section 3. Section 7 gives P3 a brief, including one hole in my own section 9.3 that needs a ruling.

## 1. What I checked

| Claim | Result | How |
|---|---|---|
| 1685 tests | **Confirmed.** 1685 passed, exit 0, 214.8 s | `pytest -q` at 2a13fe3, voice extra present |
| `make lint` exit 0 | **Confirmed** | exit code read directly |
| `make verify-range BASE=b3e9c4a` | **Confirmed.** Exit 0, every commit green | ran the target itself, table below |
| UPSC md5 `826ef550999cc34d9018806dce1b665e` | **Confirmed** | `cli --fake --script examples/fake_session.txt`, `grep '^\['` |
| Debate md5 `f2a23f33b90df75103f505a2696e6944` | **Confirmed** | same method on `fake_debate.txt` |
| `requests.py` unchanged since bd44810 | **Confirmed**, empty diff | `git diff bd44810 HEAD -- src/mockboard/brain/requests.py` |
| Hook switched on here | **Confirmed**, `core.hooksPath` is `.githooks` | `git config` |

| commit | pytest | ruff |
|---|---|---|
| effefa9 | 1441 passed, 1 warning in 215.75s (0:03:35), exit 0 | exit 0 |
| bc800c1 | 1457 passed, 1 warning in 213.98s (0:03:33), exit 0 | exit 0 |
| cadca61 | 1558 passed, 1 warning in 214.72s (0:03:34), exit 0 | exit 0 |
| 9a09569 | 1685 passed, 1 warning in 216.67s (0:03:36), exit 0 | exit 0 |
| 2a13fe3 | 1685 passed, 1 warning in 217.01s (0:03:37), exit 0 | exit 0 |

The range also ran the docs commit 2a13fe3. Case growth by commit: +14, +16, +101, +127. The last is mostly `test_formal_register_only` growing from 33 to 153 cases.

Commit 4bdbc15 (docs only) landed from elsewhere during this review, and two docs files were staged by someone else. Neither is mine, and neither affects the chunk. The commit is outside the reviewed range.

**Mutation check.** 24 mutations on an export of HEAD: 21 caught, 3 survived.

| Area | Result |
|---|---|
| The five P2 survivors (`_MAX_TERM_WORDS`, `_TERM` characters, `_fill` fallback, `CLOSED_STARTS`, `crc32`) | **All caught** |
| Grow order (both halves), `_TERM_SPACES`, cap under 5, copy of defaults, `KeyError`, dotted name, "Chaliye" filler, an informal "tum" in a seed, `_MAX_TERM_WORD_LETTERS` | All caught |
| Hook: copy of the working tree, no ruff, `pytest \|\| true`, no `cd` to the export | All caught |
| verify-range: always exit 0, copy of the working tree | Both caught |
| `>=` to `>` in the `capped_budgets` default check | Survives. **Equivalent mutant:** at 15 both paths return the defaults. No test needed |
| Hook: import proof removed | Survives. Low risk, because `PYTHONPATH=src:.` wins over the editable install anyway |
| Hook: `unset GIT_DIR ...` removed | Survives, because the tiny test project makes no git calls. The real suite's nested git calls are why it exists. Leave as is |

## 2. What is genuinely good

- **The hook tests the index and proves it.** A commit from a subfolder, `commit -a`, `commit <path>` and `--amend` all behaved correctly on trial.
- **`test_commit_hook.py` drives real git** in a throwaway repository, and adds only 5.8 s. Because it uses the exported hook, a staged hook change tests itself.
- **`_GROW_ORDER` is exact**, and the sum test across caps 5 to 15 would catch a change to the defaults.
- **`test_situation_index_is_the_crc32_of_the_form` is the right fix** for a bug an in-process test could not see.
- **`test_register_check_reads_the_seeds` tests the test.** It proves that the scan contains seeds and that the regex can fire in both scripts.
- **S5 opens nothing.** Brackets and slashes become spaces before the same allowlist runs, so `</system>` is still refused for its `<`, and the word limit still applies.
- **Commit bc800c1 explains** why the old board keeps "Dr. Iyer": the golden depends on it. `personas/economist.yaml` is the only dotted title left in `src`.

## 3. Question 1: suite size and the hook

### Is the suite bloating?

**In cases yes, in time no. The new cases are not what costs the 3.6 minutes.**

| Measure | Value |
|---|---|
| Cases / distinct test functions | 1685 / 704 |
| Wall time / CPU time | 216 s / 24 s, so **about 89% of the run is waiting** |
| `tests/voice`: share of cases / share of time | 141 cases (8%) / 185.5 s of 209 s (88%) |
| 42 setup/call/teardown phases of 1 s or more | 184 s of 209 s |
| `test_commit_hook.py`, new in this chunk | 5.8 s |

Timed by `--durations=0` on an export. The largest files are `test_brain_processor.py` (84 s), `test_timbre_tts.py` (43 s), `test_barge_in.py` (25 s) and `test_prisma_stt.py` (16 s). `test_agenda.py` and `test_panel_personas_lines.py` do not reach the top 14 files, and neither does anything else this chunk added apart from the hook tests.

**Advice on loop-inflated parameters: do not trim for speed.** The 258 new cases cost about 6 s, nearly all of it the hook file. `test_formal_register_only` (153 cases) could be one assert listing offenders. That is tidier but cosmetic, so do it only if someone is in that file anyway. **Report both numbers from now on:** "1685 cases, 704 functions". The headline count should not be read as coverage.

**The real saving is in `tests/voice`.** These tests wait on real timers. In one 6.17 s Timbre run, measured on an export, **3.97 s was `Harness.__aexit__`**, waiting for the pipeline to finish after `EndFrame`. Four Timbre tests sit at exactly 6.06 s, so this looks shared. `# UNVERIFIED:` the cause inside Pipecat. I timed the wait; I did not trace it.

*Recommendation:* one small tests-only chunk, time-boxed to an hour, after P3. Find that teardown wait, and shorten scripted delays where a test asserts order rather than duration. Under 2 minutes looks reachable, which is my estimate and not a measurement. Do not add `pytest-xdist`: it is a new dependency, and these tests depend on timing. Do not run a subset in the hook, because that is how a false green gets built.

### Is the hook sound? Can it give a false green?

**Sound for what it covers.** It exports the right tree, fails closed without a Python, and exits non-zero on a ruff or pytest failure. The export also exposes tests that lean on ignored files, since `.env` and `traces/` are absent there.

**Yes, false greens are possible.** Tried in a throwaway repository unless marked:

| # | Route | Result | Backstop | Action |
|---|---|---|---|---|
| G1 | `git merge --no-ff` of a red branch | **Hook does not run, commit made** | `verify-range` | Add `.githooks/pre-merge-commit`, below |
| G2 | `git cherry-pick` or `git rebase` of red commits | **Hook does not run, commit made** | `verify-range` | None. Nobody here should rebase. State it in the log |
| G3 | A venv without the voice extra (reasoned, not run) | 13 `importorskip` calls silently drop up to 141 cases, and the hook is green. `make venv` does not install that extra | None | Fail closed, below |
| G4 | `--no-verify` | Works as ever | `verify-range` | **The ban is not written where Ruling 60 meant it**, below |
| G5 | Editing the hook without staging it (reasoned, not run) | The working-tree hook runs, not the indexed one | `verify-range` | None |

G1 matters soon, because the web branch is to merge into this history (Ruling 43). The fix is two lines, trialled here: the red merge stopped with exit 1.

```bash
#!/usr/bin/env bash
exec "$(dirname "$0")/pre-commit"
```

G3 needs one line in both `.githooks/pre-commit` and `scripts/verify_range.sh`, with a test: `"$python" -c 'import pipecat, pipecat_gnani'`, failing with "run make voice-install".

G4: `.claude/agents/builder.md` has no mention of `--no-verify`, and neither does `CLAUDE.md`. CTO_NOTES_12 section 5 item 2 asked for it in `builder.md`. Today it is only in the README and a hook comment. This is the orchestrator's line to add, with the owner's agreement as in Round 27.

The hook does not run `eval-gate` or `eval-debate`. That is right for a commit. They belong at review.

### Is the hook proportionate? Can a docs-only commit skip tests?

**Proportionate for code commits, yes: it enforces the one thing a rule failed to. A docs-only skip is safe if it is an allowlist of paths**, and it would matter: **18 of the last 60 commits touched only `docs/`**, so 30% of commits wait 3.6 minutes for nothing.

Evidence: I deleted `docs/`, `README.md` and `CLAUDE.md` from an export and ran everything: **1685 passed**, exit 0. No test reads them.

**Rule: skip only if every staged path is under `docs/`, or is exactly `README.md` or `CLAUDE.md`. Never use `*.md`.** `src/mockboard/prompts/*.md` are prompts: `tests/test_modes.py` line 130 hashes them and `tests/test_debate_report.py` line 374 reads the changelog.

Prototype, trialled (without the two exact file names): docs-only gives skip, a prompt `.md` gives full.

```bash
git rev-parse -q --verify HEAD >/dev/null || full=1            # first commit
[ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ] && full=1       # a merge is never docs-only
paths=$(git diff --cached --no-renames --name-only HEAD)
[ -n "$paths" ] || full=1                                      # message-only amend, empty commit
echo "$paths" | grep -qvE '^(docs/|README\.md$|CLAUDE\.md$)' && full=1
```

Known effect: `--amend` adding docs to a code commit also skips. That is safe, since the tree differs from the gated HEAD by docs only and no test reads docs. `--no-renames` makes a move out of `src` list both paths. `verify-range` stays the full check at review, and it still tests docs commits, so a skip cannot fool its table. A future test that reads `docs/` would break this rule, so `test_commit_hook.py` should carry the same allowlist in a comment.

Tests to add: docs-only skips; docs plus one source file runs in full; a prompt `.md` runs in full; a red merge is refused; the first commit runs in full. This amends Ruling 60, so it is the orchestrator's call.

## 4. Question 2: read aloud as an aspirant

**Overall: better. The hand-off now sounds like one sentence from one person.**

| Line | Reading |
|---|---|
| "Dhanyavaad. Shrimati Menon, please." | Good. Formal, natural for a board |
| "Dhanyavaad. Doctor Iyer, please." / "Professor Sen" / "Shri Rathore" | Good. All stay whole in each mix |
| "Thank you. Shrimati Menon, please." | Fine in Indian English |
| "धन्यवाद। Shrimati Menon, कृपया।" | **Weak: a Latin name in a Devanagari line.** It reads badly on a caption, and `# UNVERIFIED:` how Timbre speaks it at `hi-IN`. Not a regression: "Smt. Menon" had the same problem |
| "Agar civil services ka rasta na bane, to aap samaj ke liye kya karna chahenge, aur kyun?" | Good. A real board question, and much better than "why" twice. "rasta na bane" is a touch bookish but clear |
| en: "If the civil services do not work out, what would you want to do for society, and why?" | **The builder's wording is good**, more idiomatic than a literal rendering |
| hi: "अगर सिविल सेवा का रास्ता न बने, तो आप समाज के लिए क्या करना चाहेंगे, और क्यों?" | Good, and consistent with the Hinglish |
| "Ji, achha." | Good. Short and neutral, safe before a probe or after "pata nahi". Slightly deferential for a Chairperson, but a board that says "aap" can carry it |
| "Zaroor, sawal phir se suniye." / "ज़रूर, प्रश्न फिर से सुनिए।" | Good |
| "Certainly. Here is the question again." | Good |
| "How would you deal with both sides and move forward?" | Good |
| "Aapko chess se kya seekh mili, aur woh aapke kaam kaise aayegi?" | Good. "aayegi" agrees with "seekh" |

Four things the reading turned up, none blocking:

1. **"Ji, achha." cannot be heard yet.** `filler_lines` has no reader in `src`, only the schema field, because V5 masking is not built. S7 is correct but silent until then.
2. **A one-word Timbre request survives.** M1 fixed "Smt.", but "Dhanyavaad." and "Certainly." remain their own requests under `TTS_REQUEST_UNIT=sentence`, and `test_handoff_stays_whole` pins that split, as I specified. A fixed line needs no streaming since its text is known, so P8 should send it as one Timbre request. `# UNVERIFIED:` whether that is audibly better. The voice lab can answer it.
3. **My M2 wording sounds slightly deaf after a good answer.** Seed 1 asks which issue the candidate followed, then seed 2 says "Apni pasand ke kisi rashtriya mudde par...", right after they named one. It is correct after a skip, which was the defect. P4's actor should carry the named issue across. The verbatim fallback is acceptable. At cap 10, test users do hear this pair.
4. **Test users never hear S1.** `form_hr` has budget 1 at cap 10, so the second form seed is reached only at cap 11 or above, or uncapped.

**"Achha, samajh gaya."** is masculine and sits in the Chairperson's filler list. It is fine with both male candidate voices, and wrong if the owner picks a female Chairperson. `samajh aaya` removes the question. Fix it when V5 wires fillers.

**Voice lab additions for the owner**, all `# UNVERIFIED:`: "Shrimati", "Doctor Iyer", "BTech CSE" ("BTech" comes from stripping the dots and may be spoken as one word), and a Latin name inside a Devanagari line.

## 5. Question 3: `capped_budgets`

**Matches section 9.2 exactly, and rejects below 5.** Run directly. Order is `form_hr, home, degree, interests, current_affairs`.

| Cap | Budgets | Sum |
|---|---|---|
| 5 | 1/1/1/1/1 | 5 |
| 10 | 1/2/2/2/3 | 10 |
| 14 | 2/3/3/3/3 | 14 |
| 15 | 2/3/3/3/4 | 15, equal to the defaults |
| 16, None | 2/3/3/3/4 | 15 |
| 4, 0, -1 | `ValueError` | |

Caps 6 to 13 also sum correctly. `_GROW_ORDER` has length 10, the defaults' total minus 5. At cap 5 the `must_ask` seed is present at index 2 of current affairs with budget 1, so nothing is lost in the agenda. Choosing it is P3's job (section 7, item 3).

## 6. Should-fix, none blocking

| # | Where | What | When |
|---|---|---|---|
| 1 | `.githooks/pre-merge-commit` (new) | G1, two lines | Before `web-frontend` merges |
| 2 | `.githooks/pre-commit`, `scripts/verify_range.sh` | G3, voice extra fails closed | Same small chunk |
| 3 | `.claude/agents/builder.md` | G4, write the `--no-verify` ban down | Orchestrator |
| 4 | `tests/test_commit_hook.py` | The hook's comment says `commit -a` is covered, but no test does it. Add red `commit -a` and red `commit <path>`. I checked both by hand | Same small chunk |
| 5 | `.githooks/pre-commit` | Docs-only skip, if ruled | Same small chunk |
| 6 | `tests/voice` | Teardown wait, time-boxed | After P3 |

## 7. Question 4: P3's brief, beyond section 9.4

**Item 1 needs a ruling. The rest are instructions.**

1. **Gap in my section 9.3: `end` with no free turn left, on the last budget unit.** The `end` turn counts and spends the last unit, and confirmation then asks for a turn beyond `max_answers + PANEL_FREE_TURNS`. *Proposed narrow exception to Ruling 52:* if this turn would spend the last unit, close at once with `end_request` and no confirmation, since nothing is left to resume. Anywhere else, confirmation stands. Test: `turns <= max_answers + PANEL_FREE_TURNS` in that state.
2. **Silence is `word_count == 0`, not `rule_id == "R3"`.** R3 has three meanings in `strategist.py`: silence, long pause, and first honest "pata nahi". Only silence is free. The other two count. R1 is unique. One test for each.
3. **One `next_seed` function chooses by budget left, not by index.** At budget 1 it returns the `must_ask` seed at index 2. Test caps 5, 6, 7, 10, 15 and None.
4. **The opener consumes `form_hr` seeds[0].** The seed pointer starts at 1 there, or the same person asks it twice. At budget 1 the first answer goes straight to hand-off.
5. **Repeat replays a stored `last_question`,** never the last line spoken. It excludes prefaces, acks, the repeat prefix and fallback lines. Test: two repeats give identical text with no stacked prefix, including just after a hand-off.
6. **A preface has speakers.** `change_topic` is an ack by the current member, then a hand-off by the Chairperson (`panel_lines.py` docstring). `PanelDecision` should carry ordered `(member, text)` parts now, so P8 does not rewrite it.
7. **Answers may finish under the budget total.** `change_topic`, R5 and a skip with no seed left forfeit budget, with no redistribution. My 9.1 "finish together" holds only for a plain run. Assert `<=` in general, and `==` only for plain answers.
8. **Read budgets only from `agenda.segment(id).budget`.** Never read `DEFAULT_SEGMENT_BUDGETS` and never recompute from the cap. `Agenda.segment` now raises `KeyError`, so a bad id must reach the `director_error` fallback.
9. **`session.py` line 723 rewrites the move through `cap_move` before the hook sees it.** `decide_panel` must not read that as a normal `move_on`. Close it with `ceiling`.
10. **Keep:** `requests.py` frozen (Ruling 57), both md5s, and names only from `persona.name`.

**For later chunks:** `actor.v4` uses undotted names, and P4 tests that a "Dr." from Nova does not split a line. `capped_budgets`' `ValueError` is caught at config load, never inside a session (P7).

## 8. Disagreements

None with Rulings 59, 61 and 62. On Ruling 60, the hook and `verify-range` are done, and the ban is not yet where the note asked (G4). Section 7 item 1 asks for a narrow exception to Ruling 52. The builder's single commit cadca61 ("these share one file") is reasonable.
