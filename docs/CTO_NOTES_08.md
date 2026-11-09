# CTO Notes 08: Review of chunk P0.1 (Nov 1, 2026)

**Scope.** Commits d83def0, c003187, fb85f16 on `panel-phase`, base 620eac9, against CTO_NOTES_07 and Ruling 44. No source edited, nothing committed or pushed, no remote changes. **No real Gnani or Bedrock call (Ruling 41).** Every check ran on exports under the session scratchpad.

**Verdict: ACCEPT P0.1.** It does what Ruling 44 asked, and every claim held. One tests-only chunk (P0.2, section 4) is **required before P4 starts**. It does not block P1 to P3. The gaps it closes come from my own spec in CTO_NOTES_07, not from a builder error.

## 1. Claims checked

| Claim | Result |
|---|---|
| 851 tests pass | **Confirmed** at fb85f16, voice extra present, Python 3.14 |
| `make lint` exit 0 | **Confirmed**: `ruff check --no-cache src tests eval`, exit read directly, with pipefail set |
| UPSC board md5 `826ef550999cc34d9018806dce1b665e` | **Confirmed** |
| Debate board md5 `f2a23f33b90df75103f505a2696e6944` | **Confirmed** |
| Goldens unchanged | **Confirmed**: no diff in `tests/golden` or `data/` from 620eac9 |
| Each commit green alone | **Confirmed**: d83def0 841, c003187 846, fb85f16 851; lint exit 0 and pytest exit 0 on each. Base 620eac9 is 840 |
| `make eval-gate`, `make eval-debate` equivalents (fake) | Both pass |
| Frozen cards are the 7d94936 cards | **Confirmed**: `tests/data/lock_cards` matches `data/cards` (only `README.md` differs); `tourism.json` also compared to 7d94936 |
| New baselines were not taken from changed code | **Confirmed**: I recomputed the requests, templates and real-prefix hashes on 620eac9 with only the one-line `max_tokens` pass-through. All three match the constants in the test file |
| Hashes stable | **Confirmed**: `tests/test_modes.py` green at hash seeds 0, 1, 42, and with `TZ=Asia/Tokyo LC_ALL=C`, from another folder |
| A real-shaped card in `data/cards` leaves the suite green | **Confirmed**: 851 passed with one added to `tourism.json`, board md5s unchanged |

## 2. What is genuinely good

- **The docstring now tells the truth.** It lists what is locked and what is not, unprompted. That is the correction CTO_NOTES_07 wanted.
- **`DEBATE_REQUEST_SHAPE` beside the hash.** Not asked for, and useful: a hash failure says only "something moved", while the shape test says which call and which cap.
- **`load_form_file` fix is the narrow one.** `_form_values` keeps its cut for typed forms, and a test pins that.
- **Discipline on baselines.** "Taken at 620eac9" is stated and is true.
- **The lock is fast.** All 20 tests in `test_modes.py` run in about 0.35 s.

## 3. Your questions

### Q1. Does M1 close? Is the gap acceptable?

**M1 as specified closes.** All five original mutations now fail, re-run on my own temp copy:

| Mutation | Caught by |
|---|---|
| `build_listener_user` text | `test_debate_request_hash_is_locked` |
| `build_debate_actor_user` closing line | same |
| `"round"` dropped | same |
| `actor_max_tokens` 120 to 60, also to 119 and 121 | hash and shape tests |
| Template line in `modes/debate/actor.py` | `test_debate_templates_hash_is_locked` only |

**The builder's move_on caveat is correct and harmless.** Debate's cap is `max(actor_max_tokens, cap * 3)`. Only move_on (35 words, 105) sits on the floor among the scripted moves. That one row catches a change of even 1. Rebut at 45 words would give 135, so the setting does not reach it anyway.

**The gap is wider than the builder reported.** I ran each of these against all `tests/test_debate*.py`, `test_modes.py`, `test_llm.py`, `test_motions.py`. G5 and G6 were also run together on the **full suite: 851 passed**.

| Mutation | Result |
|---|---|
| G1 `answer_first` key renamed (rebut D7) | Not caught |
| G2 `cross_on` key renamed | Not caught |
| G3 `claim_to_revisit` tag renamed | Not caught |
| G7 rebut word cap 45 to 40 | Not caught |
| G8 cross-question word cap 35 to 30 | Not caught |
| **G5 debate sent the UPSC `LISTENER_TOOL`** | **Not caught, full suite green** |
| G6 `stream_line` caps at `max_words + 5` | **Not caught, full suite green** |
| G4 default temperature 0.3 to 0.9 | Caught only by a `test_llm.py` request test |

**My ruling on the question.** G1, G2, G3, G7 and G8 alone would be tolerable through P5, because they sit in debate-owned files that P4 and P5 do not list. But three things change the answer:

1. **G5 is the serious one.** The tool schema is hashed, but nothing proves debate *uses* that tool. Fake responders ignore the tool, so a swap passes everything. On real Nova the listener would stop returning `arguments`, `rebuts` and `question_to_opponent`, and the debate would quietly lose its brain. P5 adds `PANEL_LISTENER_TOOL` and edits this wiring's neighbourhood. This is my omission: my spec's tuple had no tool name.
2. **P4 edits shared helpers these branches import.** `modes/debate/actor.py` imports `ACKNOWLEDGE_LINES`, `challenge_facts`, `fact_challenge_template` and `guard_fact_challenge` from `brain/actor.py`, and P4 changes "preface rules" there. The scripted debate never reaches D4 or D9, so those paths are dark.
3. **Rebut is debate's default move (D12), and the lock has never seen it.** A lock that skips the commonest move is a weak claim to carry into the risky phase.

**So: do not rewrite `examples/fake_debate.txt`, and do not change the golden. Add two test-only scripts.** It is cheap, and I trialled it (section 4). G1, G2, G3, G7, G8 and an `ACKNOWLEDGE_LINES` edit all moved the fingerprint in the trial.

**G6 is separate:** no test pins the stream cap at `stream_line`, because later guards trim the text in fake runs. See S2.

### Q2. Keep `test_debate_templates_hash_is_locked`?

**Keep it.** It is the only test that catches the fifth M1 mutation, and the request hash cannot see it by design: most templates are spoken only on actor failure. It reaches into private `_T` and `_Q`, which is acceptable for a lock in the same repository. It does not cover every spoken string, and ADR text should not imply that it does.

### Q3. `examples/fake_panel.txt` named for a panel that does not exist

**Fine.** CTO_NOTES_07 S4 and the P4 file list both name that path, and it is honest as an answers-only script. One trap: `test_answers_only_example_gives_the_same_board_lines_as_the_form_script` pins this file to md5 `826ef55...`, but P4 will rewrite it for panel scenarios. When that happens, P4 should keep today's answers-only script under a different name for this test, or drop the test with a stated reason. The hash must not be "updated" to pass.

### Q4. `test_eval_gate.py` reads the real `data/cards`

**Acceptable, keep it.** The subprocess test builds its own `env`, so `CARDS_DIR` does not reach it. Trial results:
- A **valid** real-shaped card: green. The gate falls back to built-in cards, because the items cite built-in ids.
- A **malformed** `tourism.json`: exactly 2 failures, this test and `test_schemas.py::test_data_cards_load_if_present`.

A malformed real card should be red, as the gate and the app genuinely cannot run with one. It is not a lock and teaches no bad habit, since the only fix is to repair the card. **Found on the way:** `make cli-fake` and `make eval-gate` die with a raw `JSONDecodeError` traceback on such a card. Worth a friendly `load_cards` error naming the file when the owner starts authoring cards. Not P0.1 scope.

### Q5. The stream-cap test's red

**Confirmed: it fails for the right reason.** With only the `llm.py` line reverted in a temp copy:

```
tests/test_llm.py:340: assert llm.calls_named("actor")[0].max_tokens == 120
AssertionError: assert None == 120
```

It is the intended assertion, not an import or signature error. The shape and hash tests also go red, so the fix is covered three ways.

### Q6. Python 3.12

**A full 3.12 run is not available offline without installing.** The interpreter exists: `/Users/yashpratapsingh/.local/bin/python3.12` (3.12.14). It has no project packages. The uv cache holds `pydantic-core`, `PyYAML`, `rpds-py`, `numpy` and `websockets` for cp314 only, and has no `langgraph`, so an offline resolve fails.

What I could do with nothing installed:
- `python3.12 -m compileall -q src tests eval probes spikes`: **exit 0**, so no 3.13+ syntax.
- A grep for common 3.13/3.14-only APIs found none. This is not exhaustive.

**The suite itself remains unrun on 3.12. Marked UNVERIFIED.** EC2 Ubuntu 24.04 ships 3.12 (Ruling 23), so a run needs a networked install in a throwaway venv, on the owner's approval, before V8 deploy at the latest.

## 4. Required before P4: chunk P0.2 (S, tests only apart from one field)

1. **Tool name in the fingerprint.** Add `tool: str = ""` to `FakeCall`, record `tool.name` in `_tool_call`, add it to the rows and the shape. **Trialled:** the fingerprint then detects G5. Add `temperature` the same way, as `stream_text` receives it. The hash changes once. Take the new baseline on code where debate has not changed, and confirm the shape's existing columns do not move.
2. **Two test-only scripts** under `tests/data/`, not `examples/`, with their own request hashes. Neither touches the golden.

Script A, motion 1, reaches `cross_question` D10, `rebut` D7, `rebut` D4, `move_on` D6 (13 calls):
```
1 / for / standard / hinglish / logician
Main motion ke paksh mein hoon. Phones class mein dhyan bhatkate hain, kyunki notifications aur games har waqt padhai todte hain.
Discipline sabse zaroori cheez hai.
Aap kya kehna chahte hain, kya phones se bachche sach mein seekhte hain?
Mujhe nahi pata is par.
Bachche ka dhyan rehta hai kyunki class mein koi phone nahi hota aur teacher par focus banta hai.
Closing mein main kahunga ki padhai ka samay padhai ke liye hona chahiye.
```
Script B, motion 5, reaches `fact_challenge` D9, `cross_question` D10, `rebut` D12 (12 calls):
```
5 / for / standard / hinglish / logician
Tourism GDP ka 30 percent hai in 2023, isliye yeh growth ka main engine hai kyunki jobs bhi wahi deta hai.
Local rozgaar badhta hai kyunki homestays aur guides ko kaam milta hai, isliye yeh zaroori hai.
Community ko fayda hota hai kyunki paisa gaon mein rehta hai, isliye yeh sahi hai.
Rules zaroori hain kyunki environment bachana hai aur isliye cess lagana chahiye.
Closing mein main kahunga ki yeh sahi hai.
```
(The first line of each is five separate setup lines.) Script B uses synthetic cards, so it runs on the fake path only, and a real run with no real cards never reaches D9 (Ruling 36). It is still the only cover of `challenge_facts`. The trial used my own seed-free fingerprint script; the builder must re-derive both baselines from unchanged code.

3. **Update the docstring** to list the moves now covered. Moves still unreached remain named (`steelman`, `redirect`, `reassure`, `clarify`, `interrupt`). I did not attempt them.

**Verify:** G1, G2, G3, G5, G7, G8 fail; both goldens and md5s unchanged; `make test`, `make lint` on each commit with real exit codes.

## 5. Should-fix (small, may ride with P0.2)

- **S1. Sentinel in the hobby test (`tests/test_cli.py`).** `assert "dd" not in str(exc.value)` checks a message that contains the temp path, so it fails on any machine whose temp path contains "dd". Reproduced with `--basetemp` on a folder with "dd" in its name: both cases fail. It is green on this Mac only by luck of the `TMPDIR` letters. Use a distinctive value such as `"zzqhobby"`.
- **S2. Pin `stream_line`'s cap.** One test asserting that `stream_line` yields at most `max_words` words. G6 shows it is unpinned, and P4 edits this file. In voice this is the path to Timbre, which is where a long line costs most.

## 6. Disagreements with rulings

None. One correction to my own notes: the request tuple in CTO_NOTES_07 M1 omitted the tool, and my "limit to state" treated the unreached moves as acceptable. Having tested it, I no longer think so past P3.
