# CTO Notes 11: Review of chunk P1.2, request detector, last round (Nov 1, 2026)

**Scope.** Commits 5d43ca6 to d47a2cf on `panel-phase`, base 47a17a6, against CTO_NOTES_10 and Rulings 55 to 57. No source edited, nothing committed or pushed, no remote changes. **No real Gnani or Bedrock call (Ruling 41).** Experiments ran on copies in the session scratchpad. No fourth holdout was written. Every line here is synthetic and mine.

**Verdict: REJECT, for one defect, with a 3-line fix and no further tuning.** All the requested work is correct. F1, F2 and F3 are closed. But a strong false hit survives in the `cheez` branch of `change_other_topic`, and P1.2 widened it. It has the same harm as F1: no listener can veto it, and under P4 it bans the candidate's current topic. This is the bar you set. The miss is mine: section 7 of CTO_NOTES_10 told him to add these tails, and I tested only `topic`. He followed the note.

**After F4 lands, the orchestrator can accept without another CTO round, and Ruling 57's freeze starts.**

## 1. Claims checked

| Claim | Result |
|---|---|
| 1194 tests | **Confirmed**: 1194 passed, exit 0, bash with pipefail |
| `make lint` exit 0 | **Confirmed**, same shell, "All checks passed" |
| UPSC md5 `826ef550999cc34d9018806dce1b665e` | **Confirmed**, from a fresh fake run |
| Debate md5 `f2a23f33b90df75103f505a2696e6944` | **Confirmed**, from a fresh fake run |
| F1 closed | **Confirmed**: all CTO_NOTES_10 Q4 lines are none or weak. The two `alawa` lines are weak. No strong hit remains on them |
| F2 closed | **Confirmed**: "sir aap kuch bhi puchiye" and Devanagari form are none. "tourism ke alawa kuch bhi puchiye" still fires with its term |
| F3 closed | **Confirmed**: h046, both `hum` lines and "can we end the session" fire. "we want to end the session" stays none |
| Dev `end` 21/21 exact | **Confirmed**, and pinned with no floor. Whole dev table: 70/70, 35/35, 21/21, 34/34, 39/39, negatives 118/118 |
| Spent `t` set | **Unchanged**: same 8 misses, 25/25 negatives, so no targeted spellings |
| Weak set is seven | **Confirmed**, pinned by test |
| Weak set "exported for P5" | **Not literally.** `_WEAK_CHANGE` is private. This does not matter: P5 reads `hit.strength`, and must not import the set |
| `word_count` exists | **Confirmed**, public, shares the detector's tokeniser |
| Thank-you trap | **Closed** on my two lines, without `you` as a filler |
| Unwired | **Confirmed**: nothing in `src` or `eval` imports the module |

## 2. Why +125 tests?

**Real, not inflated.** All 125 are in `tests/test_requests.py`, which went from 218 to 343. They are 30 new test functions with hand-written parameters. The diff adds no template loop, and the largest new table has 11 rows. No test asserts only "no crash". The corpus file did not change.

**Mutation check on a copy: 16 of 18 caught.** That is better than P1.1, and every judgement-call behaviour below is pinned. The two survivors are one issue, in section 4.

## 3. Must-fix

**F4. `requests.py`, `change_other_topic`: split out `cheez`.**

"Cheez" means "thing", not "topic", so this has F1's cause: a pattern that names no topic but counts as strong. At d47a2cf each of these, closing a normal long answer, returns strong `change_topic`:

- "...isliye hamein koi aur cheez chahiye"
- "...chahe paani ho ya koi aur cheez"
- "...pehli cheez shiksha hai aur dusri cheez", which is also what a candidate says when pausing to think between points
- "...subsidy ki jagah hum koi aur cheez le sakte hain" (new in P1.2)
- "...ab hum dusri cheez par aa sakte hain", a candidate's own signpost (new in P1.2)

**Fix.** Remove `cheez` from `change_other_topic`, and add a strong pattern for it that needs a real imperative:

```python
("change_other_cheez", RequestKind.CHANGE_TOPIC,
 rf"(?<!mera )(?<!meri )(?<!hamara ){_OTHER} cheez(?: par| ke bare mein)? {_ASK_IMPERATIVE}" + _END),
```

Tested on a copy: **343 of 343 pass with no test changed, the corpus results are identical and the same 8 `t` lines still miss.** All five lines above return none. "koi aur cheez puchiye", "dusri cheez bataiye" and "kisi aur cheez ke bare mein baat karte hain" still fire. The one cost is "kisi aur cheez par chalte hain", which goes to the listener at P5. That is the right side to fail on.

**Test:** the five lines as none, and those three as strong. `_WEAK_CHANGE` stays seven, because the new pattern is strong. Nothing else is in scope.

## 4. Should-fix, same commit

Add one test line: "hum kuch aur baat karte hain aur phir ghar jaate hain" must be none. Removing `change_kuch_baat` from `_MUST_CLOSE` passes every test today. The behaviour is right but unpinned. Removing `_NO_KI_CLAUSE` from that pattern also survives, because `_MUST_CLOSE` already covers it. Leave that as it is.

## 5. Judgement calls

1. **Dropping `chahiye` from `change_kuch_aur`: correct.** "isliye hamein kuch aur chahiye" is a plain answer. The cost is "sir kuch aur chahiye" and "kuch aur sawal chahiye", which go to the listener. `chahiye` stays after `topic` and `sawal`, where the noun carries the meaning. F4 removes it after `cheez`.
2. **`here|now` on `end_en_modal`: accept.** All sibling `end` patterns carry it, and `end` is capped at 12 words. "winter session mein shall we end the session here" fires, but one spoken question fixes that. This holds only if P3 consumes `end` through `pending_end`.
3. **`id prefer` on `change_en_want`: accept.** It is how "I'd prefer" reaches this tokeniser, since apostrophes are dropped. "for my thesis id prefer a different topic" fires, but so does the `i would prefer` form that I asked for, and the word `topic` keeps this rare.
4. **Filler-skipping before `alawa`/`chhodkar`: accept.** "public service ke alawa sir kisi aur topic pe baat karte hain" needs it. It adds no new risk class: "neta sach chhodkar to kuch aur baat karte hain" behaves the same with or without `to`, and is weak.
5. **Deleting the `repeat_hi_sawal` lookbehinds: correct.** They were half-dead, because `repeat_hi` already took "agla sawal phir se boliye". They were also wrong on "aur sawal dohraiye", which means "and repeat the question". "agla sawal dohraiye" barely makes sense as speech, so choose the cheapest reading: a repeat costs one replayed line, while a skip throws a question away.

## 6. What is genuinely good

- **Exact scope.** Five commits, each matching one table row, with no wiring and no drive-by changes.
- **`test_dev_end_is_complete_with_no_floor`.** It removes the floor that hid h046.
- **`_third_person_blocks` and `_left_edge_ok`.** Better shapes than my note's wording. `strength` now flows through as data.
- **`_ONLY_FILLERS`.** A small structural answer to the thank-you trap.
- **`test_the_position_rule_uses_the_same_count`.** It proves why `word_count` must be shared, rather than just asserting it exists.
- **`test_key_stays_an_english_word`.** A guard against the exact mistake my earlier note first suggested.

## 7. Residuals, for P12 only (no action now)

- **Misses:** "wo sawal dohraiye" (`wo` is on the third-person list, and repeat keeps that guard), "mujhe nahi pata agla sawal", "kuch aur sawal chahiye".
- **Accepted strong residual:** "aapka dusra sawal" at turn end. `sawal` and `topic` stay strong because the noun is on point.
- **Accepted weak residual:** a long answer ending "neta mudde chhodkar kuch aur baat karte hain" fires weak with the junk term "hai neta mudde". P5 can veto it.
- **Latin-only guards:** see section 8.
- **Still `# UNVERIFIED:`:** everything about real Prisma text. `traces/` on this Mac held no real Nova session, so I could not try real lines.

## 8. What P3 and P4 must know about the detector

**Call.** `detect_request(text) -> RequestHit | None`, from `mockboard.brain.requests`. Pure, no I/O, about 0.6 ms on a 136-word turn on this Mac, so it is safe in the hot path. Pass the **turn's joined raw transcript with its punctuation**: commas are clause breaks and change results. Do not lowercase or clean it first.

**Result.** One hit per turn, highest rank only: end > strong change > skip > weak change > repeat > clarify.

| Field | Meaning |
|---|---|
| `kind` | `RequestKind` (StrEnum): `change_topic`, `skip`, `repeat`, `clarify_question`, `end` |
| `avoid_terms` | Tuple of strings, always `()` unless `change_topic`, and often `()` then too |
| `pattern_id` | Stable id. Put it in the trace and session report (Ruling 51). Never branch on it |
| `strength` | `ChangeStrength.STRONG` or `WEAK` on `change_topic`, otherwise `None` |

**Strength.** Weak hits are acted on like strong ones until P5, because there is no veto yet. Branch only on `hit.strength is ChangeStrength.WEAK`, so P5 adds its veto in one place. Use `word_count(text)` for the over-8-words rule, never `.split()`. Do not touch `_WEAK_CHANGE`.

**`end` (P3).** Consume only through `pending_end` (Ruling 52). The detector has no `yes` kind: "haan" and "yes" return `None`, so P3 needs its own yes check on the reply. "haan interview khatam karte hain" returns `end`, so treat `end` there as yes.

**`avoid_terms` (P4).**

- They are **as spoken**, up to 3 words each, in the script of the transcript, Latin or Devanagari.
- They are **scanned over the whole turn**, not just the matched phrase. "paani ke alawa bijli bhi zaroori hai aage badhiye" gives `("paani",)`.
- They **can hold junk**, such as "sarkar berozgari" or "hai neta mudde".
- So never put them in a prompt or a ban unfiltered. Apply CTO_NOTES_09 section 9: the 3-word and 5-term caps, letters and spaces only.

**Cross-script plan (P4, CTO_NOTES_09 section 9).** On `change_topic`, always add the current segment's label and aliases, because `avoid_terms` is often empty. Aliases come first, from `Segment.aliases` with a static `brain/topic_aliases.yaml` fallback. `term_key()` is a best-effort backstop. Match per token or phrase, never by substring, and keep vowels so that "tourism" does not ban "terrorism".

**Script limit.** Hindi in Devanagari matches like its Latin form, but the guard word lists are Latin-only. "छात्र टॉपिक बदल देते हैं" is a strong false hit, and English written in Devanagari still misses. The default is `en-IN`. If `make stt-ab` picks `hi-IN`, this needs a one-off exception to Ruling 57, which is the owner's call.

## 9. Orchestrator actions

1. One commit for F4 and the section 4 test line, then re-run `make test`, `make lint` and both md5s. No CTO round needed.
2. Record the amendment to Ruling 57: the freeze starts after F4.
3. Add `pending_end` and the section 8 notes to the P3 and P4 briefs.
