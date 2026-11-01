# CTO Notes 07: Review of chunk P0 (Nov 1, 2026)

**Scope.** Commits e8a933e, 724fea7, 2ea6558 on `panel-phase`, base 7d94936. No source edited, nothing committed or pushed. **No real Gnani or Bedrock call (Ruling 41).** All checks ran offline, on exported copies under the session scratchpad, so the working tree stayed clean.

**Verdict: ACCEPT P0, with one required follow-up chunk (P0.1) before P1 starts.** Section 3 explains why this does not reopen the commits.

## 1. Claims checked

| Claim | Result |
|---|---|
| 840 tests pass | **Confirmed**: 840 passed at 724fea7 and 2ea6558, voice extra present. 830 passed at e8a933e |
| `make lint` clean | **Confirmed** at all three commits (ruff exit 0, also with `--no-cache` on e8a933e and 724fea7) |
| UPSC board md5 `826ef550999cc34d9018806dce1b665e` | **Confirmed** |
| Debate board md5 `f2a23f33b90df75103f505a2696e6944` | **Confirmed** |
| `tests/golden/upsc_cli_fake.txt` unchanged | **Confirmed**: no diff from 7d94936 |
| `make eval-gate`, `make eval-debate` (fake) | Both pass |

**Q1, each commit green.** Yes. Each commit was exported with `git archive` and passed lint and the full suite on its own.

**The admitted lint failure is not reproducible.** e8a933e is clean, and the reflog shows no amend. Whatever `| tail` hid is not in the commit. The repository cannot say what it was. The process fault stands: the builder reports real exit codes and never reads a pipe's status.

## 2. What is genuinely good

- **Scope.** The builder went past my spec. I asked for prefix, listener tool and prompt versions. It hashed all five debate tools and the prompt **file bytes**, plus the version map.
- **The anti-regeneration test.** `test_debate_golden_file_holds_the_recorded_board_lines` is the best idea in this chunk. I changed a board line and regenerated the golden, and it still failed.
- **The hashes are stable.** Unchanged across three hash seeds, `TZ=Asia/Tokyo`, `LC_ALL=C`, and a run from another folder. The lock tests take about 0.25 s.
- **`--form-file` is careful.** The file is read before anything is built. Debate refuses it with exit 2. Refused values were not echoed in any case I tried: unknown field, bad language, long hobby, truncated JSON, non-UTF-8 bytes.
- **ADR-010** matches CTO_NOTES_06 and Ruling 42, keeps its unknowns marked, and states limits honestly. `langgraph-checkpoint-sqlite` is indeed not installed.

## 3. Must-fix

### M1. The lock does not cover the per-call user message (`tests/test_modes.py`)

**This is a gap in my spec, not a builder error.** But the file's docstring says the lock "hashes what reaches the model", and that overclaims. These mutations all **passed the 15 tests in `tests/test_modes.py`**:

| Mutation, debate-affecting | Caught? |
|---|---|
| `build_listener_user` text changed (shared by debate) | **No** |
| `build_debate_actor_user` closing instruction changed | **No** |
| `"round"` dropped from the debate actor's JSON | **No** |
| `actor_max_tokens` 120 to 60 | **No** |
| A template line in `modes/debate/actor.py` changed | **No**: the fake run prints `fake_responders.py` text, so the golden never reaches it |

**Why it matters now.** P4 and P5 edit `brain/actor.py` and `brain/listener.py`, the exact files with no lock. Fake responders do not read prompts, so a changed message cannot alter the golden. A panel change could alter what Nova receives in debate with every test green.

**Fix (P0.1).** Add a test that spies on `cli.build_llm`, runs the scripted debate, and hashes the ordered list of `(name, tag, sha256(system), sha256(user), max_tokens)` from `FakeLLMClient.calls`.
- **Prototype result:** 9 calls, and an identical fingerprint across hash seeds 0, 1 and 42. So this will not be flaky.
- `FakeLLMClient` drops `max_tokens` for streamed calls (`llm.py` line 907, `FakeCall(name, sys_text, user, tag)`). Pass it through, as the argument is available there. Otherwise the actor's cap stays unlocked.
- Correct the docstring to say what is covered.

**Limit to state in the test.** The scripted debate reaches only `listener`, `actor` and `judge_debate`, and four actor moves (`opening_statement`, `concede_point`, `move_on`, `closing_statement`). Rebut, cross-question and fact challenge stay unfingerprinted. Also, P4's `check_place_facts` in `SentenceGate` must be off for debate by construction, because a gate change cannot show in a fake run. Review that by reading.

## 4. Should-fix (in P0.1, all small)

### S1. Decouple tests from `data/cards` (your question 3)

**Do it now.** One throwaway real-shaped card in `tourism.json` turned **10 tests red**: both UPSC golden tests, the debate golden, the prefix hash, three in the debate integrity set and three in `test_ruling36_cards.py`. Only 2 of the 10 come from P0, so this coupling was already there. A new topic file changes nothing today, but it will after P7's discovery.

**Why not leave it.** ADR-010 says a hash change is "a defect to explain, not a hash to update". Adding real cards is legitimate, so the owner would hit a red lock with no honest move except updating the hash. That teaches the wrong habit, and it would land late, when real cards are being authored.

**Fix, trialled.** Copy the current `schema.json`, `tourism.json` and `internal_security.json` to a frozen `tests/data/lock_cards/`. In `_clean_env`, `monkeypatch.setenv("CARDS_DIR", ...)`. Normalise that path to `data/cards` in the two golden helpers, so the golden files stay byte-identical. Result in a temp copy with the real card still in `data/cards`: **the full non-voice suite passed.** The voice tests were not run in that trial, so the builder must. `test_schemas.py` keeps reading the true `data/cards`, which is correct.

**Then** the prefix test's docstring needs rewording, because it claims a proof about P7 that only exists once P7 is built. P7 should run discovery over the frozen folder and compare hashes.

### S2. Lock the prefix Nova actually sees

The locked hash is the fake-path prefix, with Exampleland cards (`a36d6feb232b...`, 4,352 chars). A real run has a different prefix (`1a53bb5e6c50...`, 2,808 chars, no synthetic cards). They differ only in the cards block, so persona and prompt drift is caught either way. Add a second assertion with `allow_synthetic_cards=False`. It costs one line and proves the real prefix directly.

### S3. Hobbies text over 3 items (your question 4): should-fix, not must-fix

Confirmed: `"a, b, c, d"` silently becomes `[a, b, c]`, while a list of four is refused. No value leaks, no cost, no safety issue, so it does not block. But `--form-file` is the strict path, and silent loss matters from P2, when the fourth hobby would just never be asked about.

**Fix in `load_form_file` only:** split the text there without the cut, so the schema refuses more than three. Do **not** drop `[:3]` from `_form_values`. It is shared with `read_form` and the page's `parse_form`, where the prompt says "up to 3" and a raised error would be a new failure. Add a test.

### S4. `--form-file` with `examples/fake_session.txt` (found while reading)

The obvious command feeds the form lines in as answers: "Uttar Pradesh" is treated as the first answer, and the board presses "Koi ek thos udaharan dijiye" on "Kanpur Nagar". `--help` does warn about this, but no answers-only example exists. Make P4's `examples/fake_panel.txt` answers-only, and name it in the help text.

### S5. Commit trailer

All three commits use `Co-Authored-By: Claude Opus 5.5`, while CLAUDE.md says `Co-Authored-By: Claude <noreply@anthropic.com>`. The orchestrator should settle which applies. I do not recommend rewriting history for it.

## 5. Are the locks brittle for P4 to P7? (your question 2)

**They fire when they should.** Each of these failed the expected test: one character in `shared_debate`, a moderator's name, a new property on `LISTENER_TOOL` (through the spread), the `listener_debate`, `actor_debate` and shared `librarian` prompts, a version bump, the cards block heading, and a fake line with and without regeneration.

**They do not fire on planned panel work.** All of these left the debate tests green:

| Simulated change | Debate locks |
|---|---|
| `Move` gains the 3 new members | Green |
| `ALL_TOOLS` gains a panel listener | Green |
| `personas/panel/` subfolder | Green |
| New UPSC persona file in `personas/` | Green (UPSC golden fails, as it should until P7) |
| UPSC `shared.v1` grows | Green (same) |
| Topic order sorted, as discovery will do | Green |

**Two things to expect.** Sorted topic order fails the UPSC golden, so P7 must handle that deliberately. Editing the shared `librarian` prompt trips the debate lock by design, since debate uses it. That is correct, not brittle. Not run: Python 3.12 (EC2). The hashes are over bytes, so I expect no difference, but that is unverified.

## 6. What the board actually says (your question 5)

Run: `cli --fake --form-file examples/forms/sample_form.json --script <answers only>`.

- **Identical board lines to the golden** (md5 `826ef55...`). The only output differences are the form block and the session id I chose.
- **The opener uses the form well:** Kanpur Nagar and Electronics, one open question.
- **The rest is the known Round 20 product, and tests hide how far it is from the brief:**
  - `optional_subject: Public Administration` is loaded and never used.
  - After a Kanpur opener, the board goes to "Ab tourism par aate hain", which has nothing to do with this form.
  - It ends on an unanswered "Aap apne anubhav se kya kehna chahenge?" with no close.
  - Against the section 10 checklist of CTO_NOTES_06: no ritual, no five members, no spoken hand-off.
- This is expected before P7, and P0 was not meant to change it. No regression.
- "Exampleland" appears only under `--fake`, with the synthetic notice printed.

## 7. Disagreements with rulings

None. One correction to my own notes: CTO_NOTES_06 section 7 and P0 under-specified the debate lock (M1).

## 8. Build plan for P0.1 (S, tests only apart from S3)

1. `tests/data/lock_cards/`, `conftest.py`, golden helpers (S1).
2. Request fingerprint, `max_tokens` on streamed fake calls, real-path prefix assertion, docstrings (M1, S2). Take baselines from code that has not changed debate.
3. `load_form_file` refuses over 3 hobbies as text, with a test (S3).

Verify: `make test`, `make lint` per commit, with real exit codes; both md5s unchanged; UPSC and debate golden files unchanged; adding a card to `data/cards` in a temp copy leaves the suite green; the M1 mutations now fail.
