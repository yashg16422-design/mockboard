# CTO Notes 10: Review of chunk P1.1, request detector (Nov 1, 2026)

**Scope.** Commits 8625bd9 to f4f7aca on `panel-phase`, base e63a357, against CTO_NOTES_09 and Rulings 50 to 54. No source edited, nothing committed or pushed, no remote changes. **No real Gnani or Bedrock call (Ruling 41).** All experiments ran on copies in the session scratchpad. Every line here is synthetic.

**Holdout rule for this note.** Third-holdout lines are not quoted here, because a builder may read this file. Examples below are mine. They sit close to the holdout's classes, so see section 5 on what that spends.

**Verdict: ACCEPT P1.1, with P1.2 (section 7) required before P3 or P4 consumes the detector.** M1 to M5 are closed and the work stands. This follows the Round 25 precedent, because the code is unwired and nothing needs redoing. But my own M1 fix overshot. It created a false `change_topic` on a common Hindi idiom, and that hit is strong, so no listener can veto it. This is my error in CTO_NOTES_09, not the builder's. He did what the note said.

## 1. Claims checked

| Claim | Result |
|---|---|
| 1069 tests | **Confirmed**: 1069 passed, exit 0, at f4f7aca |
| `make lint` exit 0 | **Confirmed**, pipefail set |
| UPSC md5 `826ef550999cc34d9018806dce1b665e` | **Confirmed** |
| Debate md5 `f2a23f33b90df75103f505a2696e6944` | **Confirmed** |
| `detect_request` imported only by tests | **Confirmed**: no other importer in `src`, `tests` or `eval` |
| M1 to M5 closed, my original lines | **Confirmed**: all 8 M2 lines, all M3 lines, all 5 M4 lines, both M5 lines and every M1 line behave as specified. t002 still fires |
| Section 6 should-fix lines | **Confirmed**: the 7 medium lines are clear. The low-severity lines still fire, as expected |
| Your holdout | **Confirmed**: 9/12, 6/6, 5/5, 4/5, 4/5, 0/15 fired. Total 28/33 |
| Spent `t` set | Unchanged: 12/16, 7/9, 9/9, 8/10, 6/6, negatives 25/25. All 8 P1 misses stay unfixed, so no targeted spellings |
| Dev table | **One regression not reported**: `end` is 20/21. h046 "can we conclude this session" was hit in P1 and M2 lost it. `DEV_RECALL_FLOOR = 0.95` hid it (0.952) |

## 2. Are the 156 tests real?

**Real.** `tests/test_requests.py` went from 62 to 218 collected tests.

- **46 are two template loops**: 39 for the `_THIRD_PERSON` words and 7 for sample-form values. I asked for both. They guard a word list, so the count flatters, but each is a real check.
- **The other 110 are mostly distinct hand-written lines** with exact expected kinds, plus 7 latency cases. The 35 new corpus rows are not in this count; the corpus is one test.
- **No test asserts only "no crash".** The latency tests assert only time, which is right.

**Mutation check on a copy: 12 of 15 caught.** Two survivors are below; the third is `hum`, covered next.

| Mutation | Meaning |
|---|---|
| Remove `(?!sawal )` from `change_x_chhod` | Harmless. Priority now makes skip win first, so the guard is redundant |
| Remove the `repeat_hi_sawal` lookbehinds | Dead or untested. Add "agla sawal phir se boliye" with its intended kind, or delete them |

A third mutation, removing only `hum` from `_OWNER_WORDS`, also passes all 218. That is useful: see section 3, Q1.

## 3. Your questions

### Q1. Did M2 overcorrect? Yes, in two specific places, and both came from my note.

**Why it matters more than it looks.** The listener never returns `end` (CTO_NOTES_06, repeated in CTO_NOTES_09 section 4). So an `end` miss has no backstop, while a false `end` now costs one spoken question (Ruling 52). The trade has flipped, and `end` should lean towards recall.

**Relax two guards, keep the rest:**

1. **Remove `hum` from `_OWNER_WORDS`.** It is redundant: `_DESCRIBES_A_PLACE` already clears all three habitual M2 lines. Today "ab hum interview khatam karte hain sir" and "sir hum session yahin band karte hain" both miss.
2. **Let modal openers take a bare "session".** New pattern: `can we|could we|shall we|lets|let us` + end verb + `the|this|our` + optional `mock` + `session`, closed by `_END`. "we want to end the session" stays refused, so the opposition line stays clean. This restores h046 and "can we end the session".

**Keep `ka/ki/ke`, the place list and "we want" plus "session".** Confirmation is a safety net, not a licence. A board that asks "do you want to stop?" after a sentence about Parliament looks foolish in a demo, because this board asks about Parliament.

**Prototype result:** both changes hit, all 8 M2 lines stay clear. Remaining false `end`: "hr ki taraf se hum interview khatam karte hain" and "rajya sabha session band karo". Both are cheap under confirmation; accept them.

**Dependency.** P3 must not consume `end` without `pending_end`. Add that to P3's acceptance.

### Q2. `vishay`: real gap, but the class is not the word.

`vishay` maps to `topic` correctly. "kisi aur vishay par baat karte hain" and "...chalte hain" both hit. The gap is the **verb tail** of `change_other_topic`. These English-noun forms miss as well: "kisi aur topic par chal sakte hain", "kisi aur topic par chalein", "koi aur topic le sakte hain". The fix is one line: add `chalein`, `chal sakte hain`, `aa sakte hain`, `le sakte hain`, `le lete hain`. It stays strong and `_END`-anchored.

Do it in P1.2, not P5. It is the same shape as the phrase we teach, it is cheap, and rules must work when the listener is down. All my `vishay` negatives stayed clean in the prototype.

Two more one-liners from the same class: `what (exactly )?do you mean` in `clarify_mean_start`, and a small `change_en_want` pattern (`i would prefer|id like|i want|can we have` + `a different|another|some other` + `topic`, with `_END`). Keep it to `topic`, because "another question" is a skip.

### Q3. The omitted section 8 items

Ruling 50 said "the note's should-fixes", which read wider than your brief. I advise closing the ambiguity with this table.

| Item | Decision | Why |
|---|---|---|
| 2. Tail skip after don't-know | **P1.2, narrowed** | "pata nahi sir agla sawal" is the most natural skip in the room. Use a don't-know phrase plus next/agla sawal, not my broader rule. "the next question is about water" stays clear |
| 3. `thank you` etc. in tail fillers | **P1.2** | `end` has no listener. Multi-word trap: see section 7 |
| 3. `kya`, `na` after modals | **Waive** | Rare, and `na` is also a negator. Listener |
| 4. Wider openers | **P5 listener; review at P12** | Polite English paraphrase is what Nova is good at. Bare `we should` and `we could` would fire on answers such as "as a nation we should focus on something else". Do not add without real data |
| 6. `key alawa` | **P1.2** | Loses the refused term before P4 uses it. Do not map `key` to `ke` globally, because it is an English word. Add it in `_TERM_BACK` only (verified) |
| 6. `ala wa` term | **P1.2** | One `_SPLIT_APART` row. **Builder must test the term is captured**; I did not verify it |
| 6. "tourism chhodiye" | **Waive to P5** | "chhodiye" alone is a common word. Current-segment aliases (P4) cover most cases |
| 6. "tourism nahi" | **Waive to P5** | "X nahi" as a turn collides with "pata nahi" and ordinary replies |
| 7. Every pattern wins a line | **P1.2, tests only** | 11 never win on the corpus. 7 have no test line anywhere: `end_en_bare`, `skip_agla_tail`, `change_agla_topic`, `change_en_rather`, `repeat_tail`, `clarify_mean_tail`, `clarify_hi_samjhaiye`. An untested pattern is a hidden risk. Add a line for each, or delete it if shadowed |
| Remove `sorry` from `_END` | **Waive** | Ruling 52 makes it cheap |
| Section 6 low lines | **Waive** | "enough of this politics", "enough on paper", "I do not want to talk about my success", "dalit samaj nahi aaya": short, plausible requests or cheap. P12 decides |

### Q4. Does "kisan kisi aur topic pe baat karte hain" fire? Yes, and it is **not** acceptable.

That exact line is contrived, because "topic" is rare in an answer. But the guard's removal exposed a very ordinary shape. At f4f7aca all of these return strong `change_topic`:

- "neta kuch aur baat karte hain aur karte kuch aur hain", the classic phrase for a politician who says one thing and does another
- "chunav mein neta kuch aur baat karte hain"
- "log kuch aur baat karte hain par sach yeh hai"
- "meeting mein kisan kuch aur baat karte hain"
- "debate mein panelists kuch aur baat karte hain", where the subject is on no list
- A 45-word answer ending "...aur neta kuch aur baat karte hain"
- "sarkar berozgari ke alawa kuch aur baat karte hain", which also returns avoid term `sarkar berozgari`
- "students topic badal dete hain" and "hamare gaon ke log topic chhod dete hain"

**Why this is must-fix.** The listener can add a missed request, but Ruling 53 cannot veto a strong hit. Under CTO_NOTES_09 section 9, a `change_topic` also bans the current segment. So a good answer about politics ends the topic for the whole session. That is worse than Round 20's complaint. Misses recover and false strong hits do not, so strong patterns should give up the ambiguous case.

The cause is that "kuch aur baat karte hain" carries no topic word, so it was never really strong. The fix is in section 7, F1.

### Q5. Do the labels match P5?

**Mostly. One change.** The field and pattern ids are what P5 needs. The builder's three extra weak patterns (`change_en_move`, `change_en_ahead`, `change_en_move_bare`) are correct. Ruling 53 listed three, so it needs amending, and `change_kuch_baat` (F1) joins them.

Three points for P5's design:

- **Only tail-capable patterns can ever be vetoed**, given the over-8-words rule: `change_aage`, `change_en_modal`, `change_kuch_baat`, and `change_en_move` when not at the start. `change_x_chhod` and `change_en_move_bare` are weak for priority only. That is fine, but say so.
- **`RequestHit` carries no word count.** P5 must count words the same way the detector does, or the 8-word rule will disagree with it. Pass a `total_words` field or use the same tokeniser.
- **A vetoed weak hit hides lower ranks.** "can we move on, please repeat" drops to nothing, not repeat. Accept this.

## 4. What is genuinely good

- **Scope discipline.** Seven small commits, one defect each, nothing wired, and both boards byte-identical.
- **`_MUST_CLOSE` reading `tokens.breaks`.** This is the structural answer to mid-sentence phrases, and it beats my regex suggestion. Keep extending it.
- **The priority is data, not order.** `_priority` plus `test_rank_order_is_the_ruling` means the ruling is pinned.
- **`test_every_weak_pattern_has_a_line` is exactly right.** It fails on my F1 change, as it should.
- **The corpus history is honest.** The note calls the `h` rows tuning data that "prove nothing", and the printed table says SPENT.
- **`test_long_repetition_stays_fast` holds.** No new unbounded gap; my worst case in the prototype was 34 ms at 5,000 repeats of "kisi aur".
- **No targeted spellings.** All 8 `t` misses are untouched. That restraint is what makes the numbers mean anything.

## 5. Holdout accounting

**28/33 with 0/15 false fires is the honest P1.1 number**, and it is the one to report. The false-fire count is the better news. Thirty-three lines cannot give a precise rate.

Discussing the five misses spends the set. P1.2 fixes four of their classes directly. After that:

- Mark the set **spent**. Do not re-report it as an improved score. My prototype clears all five, but that proves the fix applied, not recall.
- Do not write a fourth holdout. The next number that means anything is the real one at P12 (Ruling 54).
- Commit `holdout3.py` lines to the corpus only after P1.2, labelled spent, if you want them as regressions.

## 6. Disagreements and residuals

- **Disagreement with my own CTO_NOTES_09 M1 and section 6 line** that guards "should fail towards request for strong patterns". That holds only when the pattern names a topic. Corrected here.
- **Disagreement with accepting this as a false positive**, for the reason in Q4.
- **`kuch aur ... chahiye` should-fix.** "isliye hamein kuch aur chahiye" fires strong at the end of a long answer. Drop `chahiye` from `change_kuch_aur` in P1.2. All 218 tests other than the pinned weak-set test pass without it.
- **Accepted residuals**: `neta asli mudde ke alawa kuch aur baat karte hain` stays a weak hit after F1, with a junk avoid term, and the listener can veto it. Lists never cover every subject in the `kisi aur topic` shape; the word "topic" keeps that rare.
- **Still `# UNVERIFIED:`**: everything about real Prisma text; English written in Devanagari (if the owner chooses `hi-IN`, it becomes must-fix, per CTO_NOTES_09 section 6).

## 7. P1.2 (S, about 30 source lines, no wiring)

Prototyped on a scratch copy. Result: 217 of 218 tests pass; the one failure is the pinned weak set, which F1 changes on purpose. The F1 example lines above came back none or weak, as stated.

**Must-fix**

- **F1. `requests.py`: split the idiom out of `change_other_baat`.**
  - `change_other_baat` requires a `_TOPIC_NOUN`. Stays strong.
  - New `change_kuch_baat`: `_OTHER|kuch aur|kuch dusra` + `_BAAT_VERB`, same `ki` lookahead. Add it to `_WEAK_CHANGE` and `_MUST_CLOSE`, and apply the `_aage_is_a_request` left-edge check. Left set: `_AAGE_LEFT` plus `alawa`, `chhodkar`, `hum`. **Keep `hum` out of `_AAGE_LEFT` itself**, or "hum aage badhte hain" starts firing.
  - In `_not_a_request`, third-person words block a hit if the pattern is weak, or strong and declarative (last token `hain`), **unless** `before[-1]` is `alawa` or `chhodkar`. Imperatives stay free. All M1 tests still pass.
  - Tests: every section 3 Q4 line, the long-answer form, and "hum kuch aur baat karte hain" and "chaliye kuch aur baat karte hain" still firing. Update `WEAK_LINES`. Note "kuch aur baat karte hain" moves from strong to weak.
- **F2. `change_kuch_aur`: remove `kuch bhi`.** "sir aap kuch bhi puchiye" means "ask me anything", and it returns strong `change_topic`, the opposite meaning. `change_alawa_tail` still covers "X ke alawa kuch bhi puchiye". Pre-existing from P1, missed by me.
- **F3. Q1's two `end` relaxations.** Add a test that names h046, so a floor cannot hide it again: **for `end`, require 21/21 on dev.** One lost line must fail.

**Should-fix, same chunk**

- Q2 verb tail, `what exactly`, `change_en_want`.
- Item 2 `skip_dont_know`; item 3 thank-you tail; item 6 `key` and `ala wa`.
- Drop `chahiye` from `change_kuch_aur`.
- Item 7: the 7 untested patterns; the `repeat_hi_sawal` lookbehinds.

**Thank-you trap.** `_ends_clause` checks tokens one at a time, so "thank you" as a two-word filler fails there: "aage badhiye thank you" and "can we move on thank you sir" returned none in my prototype. Make `_ends_clause` accept it, and test those lines. Do not put `you` in `_FILLERS`.

**Commits**

1. F1 with tests.
2. F2 and F3 with tests.
3. Tails, clarify, want, don't-know, thank-you.
4. Avoid-term fixes.
5. Test hygiene.

**Orchestrator rulings needed**

- Amend Ruling 53's weak list to the seven patterns.
- Replace Ruling 50's "should-fixes" with the Q3 table.
- P3's acceptance: `end` is consumed only through `pending_end`.
- The third holdout is spent at 28/33.

**Verify for P1.2.** `make test` and `make lint` with real exit codes; both md5s unchanged; `detect_request` still imported only by tests; dev `end` 21/21; the 8 `t` misses still unfixed; a short review only, because this detector has now had three rounds. After it, stop tuning rules until real transcripts exist at P12.
