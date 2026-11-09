# CTO Notes 15: review of chunk P3 (panel director)

Reviewed `panel-phase` at 50f3411: commits b8feacb, e585ef8, 50f3411, base 6d432b9. No real Gnani or Bedrock call (Ruling 41). Nothing pushed. No source edited. My trial fixes ran on a scratch copy.

## 0. Verdict

**REJECT, narrowly, on one defect: M1.** `is_clear_yes` closes the interview on a question ("ji?", "haan?", "जी?"). A close cannot be undone and this is the exact path Ruling 52 exists to protect, so it would harm a candidate. M2 goes in the same small fix. Everything else is sound, and this is the best chunk of the phase so far.

The fix is about 15 lines, specified and trialled in section 3. As with Ruling 58, no further CTO round is needed: the orchestrator compares the diff with section 3 and re-runs the checks.

## 1. What I checked

| Check | Result | How |
|---|---|---|
| 1862 tests | **Confirmed**: 1862 passed, 1 warning, 249 s | `make test` at 50f3411 |
| Lint | **Confirmed**: exit 0 | `make lint` |
| `make verify-range BASE=6d432b9` | **Confirmed, exit 0**: 1703, 1703, 1862 passed, ruff exit 0 on each; also 4b15447 (a later docs commit), 1862 | see the note below the table |
| UPSC md5 `826ef550999cc34d9018806dce1b665e` | **Confirmed** | `cli --fake --script examples/fake_session.txt`, `grep '^\['` |
| Debate md5 `f2a23f33b90df75103f505a2696e6944` | **Confirmed** | same on `fake_debate.txt` |
| `requests.py` unchanged | **Confirmed**: empty diff from 6d432b9 | `git diff` |
| No model call per turn | **Confirmed by structure** | `PanelDirector` takes no client; all planners are pure; the fake run needs no deps |
| Latency | **p50 1.62 ms, p99 1.84 ms, max 1.96 ms** | `decide_turn` through the real graph, 450 turns over 30 sessions, this Mac |
| Scripted trace | Read in full | `PYTHONPATH=src:. python tests/director_sim.py` |
| Test strength | **20 of 20 mutations caught** | Hand mutations of `director.py` in a scratch copy, `tests/test_director.py` only |
| Fuzz | **No hard invariant broken** | 4,800 random sessions, section 5 |

**A red run that was my fault.** My first `verify-range` showed 3 failures on every commit. I had exported `AWS_PROFILE=none` as a safety measure, and that raises `ProfileNotFound`. I reproduced two of them under that value (`test_top_k_comes_from_settings`, `test_lane_clients_take_pools_and_timeouts_from_settings`), and the clean `make test` passes all 1862. The rerun in the table used the normal environment. Those tests build real boto clients and depend on a valid profile name, which is a small fragility, not a P3 defect.

## 2. What is genuinely good

- **`understand` plus planners is the right shape.** One priced classification, one planner per node, and `decide_panel` and the graph run the same code. The fallback is real, not a second policy.
- **The fold is properly idempotent.** I broke the checkpointer for two turns: every request, decision and turn id came out exactly once, in order, after recovery.
- **The trace reads like a board.** Five members, spoken hand-offs, two identical repeats with no stacked prefix, DM situation at answer 9 of 10, plain close at 10.
- **The silence test has teeth.** A fallback analysis with real speech is not silence.
- **Negation handling is better than I expected.** About 100 adversarial replies, including "I didn't want to end the interview", "interview khatam nhi karna" and "haan, lekin ek baat aur". Only the question-form replies of M1 and three harmless ones in section 6 closed.
- **The hook tests drive real git.** See section 8.

## 3. Must-fix

### M1. `is_clear_yes` closes on a question (`brain/director.py`)

These all close the interview today: `ji?`, `haan?`, `yes?`, `ok?`, `जी?`, `हाँ?`, `theek hai?`, `bilkul?`, `sure?`, and `kya interview khatam karna hai?` (through `end_hi`).

**Why it matters.** "ji?" is everyday Hindi for "pardon?". The likeliest time to hear it is straight after a false `end` detection, when the candidate is puzzled by "Aap interview yahin samapt karna chahte hain?". The candidate never asked to stop, and the board ends the session. `# UNVERIFIED:` how often Prisma writes a `?`. CTO_NOTES_11 section 8 says punctuation reaches the detector; I could not settle question marks from our traces. Without a `?` a bare "ji" still closes, and that cannot be told apart.

**Fix,** first lines of `is_clear_yes`:

```python
if text.rstrip().endswith(("?", "？")):  # "ji?" is "pardon?", and a close cannot be undone
    return False
```

**Tests:** the replies above must not close, in all three scripts, and "haan ji" still must.

### M2. `PanelDirector.finish` can lose the report (`brain/director.py`)

`adelete_thread` sits in `finally` with no guard. A failing delete raises out of `finish`, and so does a graph with no `checkpointer` attribute, even after the `except` branch has done its work. Its own comment says "the report must still come". `InMemorySaver` is unlikely to fail here, so the risk is low, but this is the path `hooks.report()` takes in P4.

**Fix:** move the delete after the `try` and guard it:

```python
try:
    await self._graph.checkpointer.adelete_thread(self.session_id)
except Exception:  # a leaked thread must not cost the candidate the report
    logger.warning("director: thread %s not deleted", self.session_id)
```

**Test:** a saver whose delete raises and a graph with no checkpointer; `finish` returns memory in both.

### Should-fix, in the same commit, because each is under five lines

- **S1. A decline before a close contradicts itself.** At t14 of section 4's second case the candidate hears "Theek hai, hum aage badhte hain. Is abhyaas interview ki tay seema yahin poori hoti hai." `plan_close` should drop the decline line: `return p.close(p.u.close_reason)`. The hand-off keeps it, where it is true.
- **S2. A counted decline should ask the situation (section 4).** In `plan_handle_request`:

```python
if kind == "end_declined":
    resume = (p.chair(END_DECLINED_LINES),)
    forced = p.forced_seed()
    if forced is not None:
        return p.ask_seed(forced, "the situation must be asked within the budget", resume)
    return p.repeat(resume, "D-RESUME", "end not confirmed; resume")
```

I trialled M1, M2, S1 and S2 together on a scratch copy: `tests/test_director.py` passes, 159 of 159, ruff clean, and the failure and scenario runs behave as described. New tests for them are still to be written.

## 4. Rulings on the builder's calls

**1. Silence: upheld.** `word_count == 0` with zero transcript words is right, for the reason in the docstring.

**2. Ruling 66 scope and `must_ask`: upheld with S2.** Reading "nothing is left to resume" as the agenda's last unit is correct. On a segment's last unit, a resume is still possible.

**At the shipped cap of 10, yes, it can be lost, despite the docstring's "at a tight cap".** It needs all four free turns gone, then `end` and a decline in `current_affairs`:

| Case | Result today | With S2 |
|---|---|---|
| `end` at the first CA answer (t12), decline t13 | Repeat, close at 10. **Situation lost** | Situation asked |
| `end` at the second CA answer (t13), decline t14 | Close `answers`, lost | Lost, accepted |
| Only three free turns gone, first case | Asked | Asked |
| Overspend in the segment before CA | Asked: CA keeps it | Same |

The fuzz agrees. Leaving aside sessions the candidate ended and topic changes away from CA, every lost `must_ask` in 4,800 sessions involved an `end` confirmation. None came from repeats, silence, garbling or skips alone. Wrap-up was not fuzzed; section 9.4 already allows it to forfeit.

The remaining case is a candidate who has used every free turn and asks to stop on the last available question. CTO_NOTES_12 section 9.4 already lets `change_topic` and wrap-up forfeit the situation, and adding a turn would break the cap. Ruling 62's "should be asked" is honoured as far as the cap allows. The docstring's "Known cost" should be reworded to this.

Side effect: an overspend makes `ceiling` reachable in **uncapped** sessions (30 of 1,600 fuzz sessions at a heavy `end` rate), because no cap guard closes first. It is harmless: same plain close, turn bound holds. But CTO_NOTES_12 calls it unreachable, so a P4 invariant "no `director_error`" would flake. See section 9.

**3. Request in a declining reply: upheld. Wrap-up on a decline: upheld.** The builder is more right than the question suggests. "nahi, chaliye aage badhte hain" is a natural decline and contains a `change_topic` phrase. Honouring it would drop a segment on someone who just asked to carry on. I read Ruling 51's "each detected request" as requests the board acted on; if the orchestrator wants the ignored ones listed, P5 can add them as a report-only row. Resuming on a due wrap-up is the only honest line after "no", and the 1020 s hard stop bounds it, provided P8 really enforces that.

**4. `gist` now, `summary` and `recent` later: upheld.** They are read-side renderings of `notes`, so P4 and P5 need no schema change. One thing for P5: counted declines and counted requests also make notes, and their gists are noise.

**5. The nine-move tests: upheld. Debate gets only its moves.** `DEBATE_MOVES` is untouched, disjointness is asserted, `eval/debate/invariants.py` reads the debate profile, the debate md5 holds, and no `Move` value enters a Nova tool schema. `reachable == RULE_TABLE_MOVES` stops the rule table emitting a panel move. This tightened the tests.

## 5. Fuzz (a driver on `tests/director_sim.py`, scratch only)

Caps 5, 10, None; free turns 0, 4; two random behaviour mixes; 400 sessions each. Rates are deliberately far above real use, so read them as reachability, not as frequencies.

Held in every session: finishes; answers within cap and budget total; turns within budget total plus free turns; free turns within limit; `move_on` only from `handoff` or `close`; no empty complete line; no `director_error` except `ceiling`.

## 6. `is_clear_yes`, in full

| Reply | Result | Right? |
|---|---|---|
| haan ji, ji haan, yes please, theek hai, ok, bilkul, जी हाँ, ठीक है। | Closes | Yes |
| haan lekin ek baat aur, yes but one more thing, हाँ लेकिन एक बात और | Resumes | Yes |
| ok continue, haan aage badhiye, sure let's move on, haan repeat kijiye | Resumes | Yes |
| ji nahi, haan nhi, ना, abhi nahi, interview khatam nai karna, इंटरव्यू खत्म न करें | Resumes | Yes |
| ji?, haan?, जी?, kya interview khatam karna hai? | **Closes** | **No: M1** |
| ok ok ok, ji ji ji, theek | Closes | Acceptable |
| **haan khatam karo, ok end it, yes end it, yes end the interview, haan band kijiye, हाँ समाप्त कीजिए, जी हाँ, समाप्त करते हैं** | Resumes | **Wrong, but safe** |

**S3, the last row.** The most natural Hindi confirmation does not close. The board says "we continue" to someone who said "yes, finish", and they must ask again. It fails safe and costs a free turn, so it blocks nothing. A closed vocabulary fixes it in `is_clear_yes` with `requests.py` frozen: at least one yes-word, at least one end-word (`khatam`, `samapt`, `band`, `end`, `finish`, `stop`, and Devanagari), and every other word from a small verb and filler list (`karo`, `kijiye`, `karte`, `hain`, `it`, `the`, `interview`). "lekin" is not on the list, so it still resumes. Optional here; if not done now, do it at P12 with real transcripts. Not trialled.

## 7. Failure and thread loss

| Injected | Result |
|---|---|
| `aput` fails for two turns, recovers | Clean. Requests honoured, nothing lost or doubled |
| `aget_tuple` fails on the first turn | Clean, recovers next turn |
| Thread deleted mid-session | Falls back every turn with `KeyError: 'memory'`; correct decisions; `finish` recovers all requests. The queue grows for the session, which is small. Acceptable |
| `finish` twice | Clean |
| Delete fails, or no checkpointer | **`finish` raises: M2** |

By reading, not by test: `CancelledError` from a barge-in is not caught by `except Exception`, and should not be. The queue is cleared only on success, so that path looks safe too.

## 8. Hook

Exercised in a throwaway repository with real git and a stand-in for the voice-extra import.

| Case | Result |
|---|---|
| Docs-only commit | Skipped |
| `src/mockboard/prompts/x.md`, `docsx/`, `README.md.bak` | Tested |
| Docs plus red source; `commit -a` with red source | Refused |
| Red source left unstaged, docs staged | Skipped, correct: the index is what commits |
| True merge of a red branch | **Refused** |
| Merge of a docs-only branch | Tested |
| Conflict resolved through `git commit`, docs paths only | Tested, through the `MERGE_HEAD` check |
| Fast-forward of a red branch | **No hook runs** |

`pre-merge-commit` works and the allowlist holds. I found no test that reads `docs/`, and every tracked file there is markdown today. Two limits, neither a defect:
- **Fast-forward:** git makes no commit, so no hook fires. Merge PR #1 with `--no-ff`, or run `verify-range` after it. Worth one line in the merge instructions.
- **Allowlist breadth:** `docs/run.py` would skip, because the allowlist is by folder, as `docs/` in Ruling 67's list implies. Harmless while no code lives there.

## 9. P4's brief, beyond CTO_NOTES_12 section 9.5

1. **`finish()` in a `finally` on every exit path,** including disconnect. The saver is shared, so an unfinished session leaks its thread for the life of the server.
2. **Free turns leave `last_move` alone.** `strategist.py` guards "no pressure twice in a row" and "no pressure right after reassure" on `last_move`. If a repeat overwrites it, as `Sim.apply` does, "please repeat" lets pressure through. Test it. Fix the sim too.
3. **Check `line_complete` before `Move.clarify`.** `D-END-CONFIRM` is a `clarify` move, and `actor.py` line 718 has its own `clarify` branch. Otherwise the confirmation may be followed by a clarify line.
4. **`next_question` and `rephrase_question` have no filler.** `actor.py` falls back to the probe template. A failed seed line should use the seed; a failed rephrase should repeat verbatim (CTO_NOTES_06 section 3).
5. **`asked` is honest.** None for fallback lines and `line_complete`. An honest-unknown or long-pause `reassure` may hold no question; `_keeps_last_question` only protects the silence case, so decide this one and test `repeat` after it.
6. **`panel.avoid_terms` is unfiltered.** Apply CTO_NOTES_09 section 9 before any prompt or gate.
7. **Do not assert "no `director_error`" in invariants.** Allow `close_reason == "ceiling"` (section 4).
8. **Set `max_turns` from the hook first,** or the server's 9 ends the panel. `cap_move` still runs before the hook.
9. **Reuse `tests/director_sim.py` as the expected behaviour,** then assert the real session gives the same decisions for the same script.

## 10. Disagreements

None with Rulings 52, 57, 61, 62, 66 or 67. One correction to my own CTO_NOTES_12 section 9.4: `ceiling` is reachable, not unreachable. CLAUDE.md's 722-test figure is stale, a small docs item.

Not verified: Prisma's question marks, real transcripts of any of these replies, and anything on a real network. Ruling 41 stayed in force.
