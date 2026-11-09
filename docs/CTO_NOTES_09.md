# CTO Notes 09: Review of chunk P1, request detector (Nov 1, 2026)

**Scope.** Commit 613c162 on `panel-phase`, base 14956d5, against CTO_NOTES_06 sections 3, 10, 15. No source edited, nothing committed or pushed, no remote changes. **No real Gnani or Bedrock call (Ruling 41).** All experiments ran in memory from the session scratchpad. Every test line in this note is synthetic and written by me.

**Verdict: REJECT P1 as it stands. A small P1.1 fixes it (section 8).** The build is clean and every reported number is true. But the detector fails the sentence shape it exists for when the refused topic contains a common word such as "public" or "government". It also fires false `end` and false `change_topic` on ordinary answers. Nothing is wired, so no user is exposed, and P2 may run alongside. **P3 and P4 must not consume this detector until P1.1 is accepted.**

## 1. Claims checked

| Claim | Result |
|---|---|
| 913 tests | **Confirmed**: 913 passed at 613c162, exit 0 |
| `make lint` exit 0 | **Confirmed**, pipefail set |
| UPSC board md5 `826ef550999cc34d9018806dce1b665e` | **Confirmed** |
| Debate board md5 `f2a23f33b90df75103f505a2696e6944` | **Confirmed** |
| Detector imported only by its test | **Confirmed**: `tests/test_requests.py` is the only importer in `src`, `tests`, `eval` |
| Recall table | **Confirmed**: dev 1.00 for all kinds. Holdout change_topic 12/16, skip 7/9, repeat 9/9, clarify 8/10, end 6/6. All 119 negatives clean |
| The 8 holdout misses stay unfixed | **Confirmed**: t006, t007, t015, t016, t017, t024, t041, t042 all still miss at HEAD |

## 2. What is genuinely good

- **Honest labelling.** The module docstring, the commit message and the printed table all say the corpus is synthetic and says nothing about real Prisma text. This is the discipline the contest rewards.
- **Unicode is done properly.** NFC normalisation on both the keys and the text, nukta forms, zero-width joiners, danda as a clause break. Few student projects get this right.
- **`end` needs the word "interview" or "session".** "I will stop here", "main yahin khatam karta hoon" and "end of the road" stay clean. This is the right instinct.
- **Your four named traps hold.** Classroom ("a good teacher knows when to change the topic"), "repeat offenders", "end of the road" and "skip a generation" did not fire, including bare and with "please" in front.
- **Negation holds.** "please dont end the interview", "interview khatam mat kijiye" and "abhi interview khatam nahi karna" all return none. Self-repair after `end` also works ("I want to end the interview no no sorry" returns none), with one exception in section 6.
- **Spoken spelling is kept for avoid terms**, and the edit-distance list is short and guarded by `_NO_FUZZY`.
- **Performance is excellent** (section 7).

## 3. Must-fix (P1.1)

### M1. The third-person guard blocks the refused topic itself
**Where:** `_not_a_request`, `_THIRD_PERSON` check on `before`.
**Why:** the guard reads the words before the phrase, and in "X ke alawa kisi aur topic pe baat karte hain" those words are the refused topic. These all return none:

- "public administration ke alawa kisi aur topic pe baat karte hain" (`public` is on the list, and Public Administration is the optional subject in `examples/forms/sample_form.json`)
- "government ke alawa kisi aur topic pe baat karte hain"
- "kisan ke alawa...", "students ke alawa...", "foreign ministers ke alawa kuch aur puchiye"

This is the owner's Round 20 sentence with a different noun. The panel's topics come from the form, so these nouns will appear.
**Fix:** patterns with explicit topic-change words (`change_other_baat`, `change_other_topic`, `change_alawa_tail`, `change_kuch_aur`, `change_topic_chhod`, `change_badal`, `change_topic_skip`, `change_agla_topic`) keep the negator and reported-speech checks but skip `_THIRD_PERSON`. Only 3 corpus lines rely on that guard at all (d145, d146, h058), and my prototype showed exempting these patterns changes none of them. Add a test running the brief's sentence over every word in `_THIRD_PERSON` and every field value of the sample form.

### M2. False `end` on real answers
**Where:** `end_hi` (no start anchor, accepts "session"), `end_en` (accepts "we ... session").
**Fired `end`, all 12 words or fewer:**

- "campus placements mein hum interview khatam karte hain"
- "company mein hum interview band kar dete hain"
- "hamare yahan interview band karte hain"
- "pradhan mantri ka interview band kijiye"
- "parliament ka session band karo"
- "sansad ka session khatam karte hain"
- "we want to end the session"
- "as the opposition we want to end the session"

The first three could be how a candidate describes a job. The UPSC board asks about Parliament, so the "session" lines are not exotic. A1b's "zero false `end`" is true only of the corpus.
**Fix:** refuse a match when the token before `interview|session` is `ka/ki/ke`, or when it is `hum` with a habitual reading. Drop `we ...` plus bare `session` from `end_en`: keep "I" with "session", and "we" only with "interview" or "mock session". My prototype of the `ka/ki/ke/hum` guard cleared the three lines I rechecked (campus, parliament, pradhan mantri) and left every corpus `end` line hitting. It will not clear "hamare yahan interview band karte hain", so add a habitual-context check for `hamare yahan`, `company mein` and similar, or accept that residue under the confirmation in section 6. Add all eight lines as negatives.

### M3. False `change_topic` on ordinary Hindi sentences, some with a permanent topic ban
**Where:** `change_x_chhod`, `change_aage`.

- **`change_x_chhod`.** "corruption ko chhodiye asli samasya gareebi hai" is the everyday idiom "leave corruption aside, the real problem is poverty". It fires with `avoid_terms=("corruption",)`, so the candidate's own subject is banned for the session. The same goes for its Devanagari form, "reservation ko chhod dete hain to bhi samasya rahegi", and "tambaku ko chhod dijiye sehat ke liye". **No corpus line ever wins through this pattern**, so nothing tested it.
  **Fix:** allow only "X ko chhodiye" as the whole turn plus fillers (remove `(?: \S+){0,5}$`), and exclude `sawal` from X so it cannot steal skip lines (see M5).
- **`change_aage`.** No `_END`, no subject check. "gaon ke bachche aage badhte hain", "bharat aur china aage badhte hain", "देश आगे बढ़ते हैं" and "aage chalte hain aur phir dekhte hain ki kya hota hai" all fire. Worse, **it fires at the end of a 33-word answer**: "...aur isi tarah gaon ke bachche aage badhte hain". A concluding sentence ends the segment. Listing third-person nouns can never be complete.
  **Fix:** invert it. Imperatives (`badhiye`, `badho`, `chaliye`) stay free. Declaratives (`badhte hain`, `chalte hain`, `badh sakte hain`) need an allow-listed left edge: turn start, a clause break, a filler, `to/toh`, `chaliye`, `nahi`, `pata nahi`. Add `_END`. My prototype cleared all the lines above but lost t002 ("sir is par aur nahi bolna, aage badhte hain") because it ignored clause breaks. The real fix must read `tokens.breaks` after the match.

### M4. "Next question" asked in Hindi repeats the same question
**Where:** `repeat_hi_sawal`, where the adverb is optional, so "sawal + any again-verb" matches; `_ASK_VERB` lacks "puch lijiye" and "bata dijiye".
**Wrong kind, returns `repeat`:** "agla sawal puch lijiye", "agla sawal bata dijiye", "next sawal puch lijiye", "kuch aur sawal puchiye", "kisi aur topic ka sawal puchiye".
The candidate asks to leave and the board says the same question word for word. For a topic-change request, that is the owner's complaint in its sharpest form.
**Fix:** require `phir se|dobara|ek baar aur` or a true repeat verb (`dohraiye`, `repeat ...`), and refuse `agla|next|aur|dusra` before `sawal`. Add `puch lijiye`, `bata dijiye`, `pooch lijiye` to `_ASK_VERB`.

### M5. An explicit topic change must outrank skip
See section 6. "is sawal ko chhodiye kisi aur topic pe baat karte hain" returns `skip` today, so the board stays on the topic.

### M6. Change the acceptance wording (orchestrator action, not builder)
See section 4.

## 4. Question 1: is "every time" honest?

**No, not as an unqualified claim, and it never was on real speech.** CTO_NOTES_06 section 3 said so, but the phrase in A1 reads wider than the proof. What each layer covers:

| Layer | What it really covers |
|---|---|
| Rules, listed sentences | The brief's sentences, in the tested spellings: reliable. The 15 sentences in `test_brief_and_design_sentences_hit` hit; my variants of the owner's sentence hit except M1 and noted cases |
| Rules, unseen paraphrases, synthetic | Holdout 0.75. My blind set **1/8** (section 5) |
| Listener (P5) | Nova only, 30 words or fewer, never `end`, and only if it returns in time. Not measured. It does nothing on `--fake`, and it fails when it is needed most, under throttling |
| Real Prisma text | **Unknown.** Round 20's Defect H recorded real `en-IN` output such as "kuchh badh mein" for "kuch baad mein". Corpus typos are far milder. One such slip, "baad karte hain" for "baat karte hain", kills `change_other_baat` |

**Do the 4 misses show a class?** Yes, two classes.

1. **Closed opener and verb lists (t006, t007).** `_ASK` is a list, so "I think we should change the topic" misses, and so do my "why don't we discuss something else" and "maybe we could shift to a different area". t006 and my "sir agriculture pe baat kar sakte hain kya" add a related shape: naming the **target** instead of the refused topic. No pattern handles this ("X instead", "X pe baat kar sakte hain"), and it needs care because "Let us talk about the farmers first" is a true negative (h069). Here the listener is the right tool, not more regex.
2. **Short verbs have no fuzzy reach (t015, t016, and t024 for skip).** `move`, `talk`, `next` are under `MIN_FUZZY_LETTERS`. Variants are enumerated ("moov", "muv", "tock"), and the holdout's "muve", "tolk", "nex" fell just outside. Do not widen fuzzy to 4 letters ("move" reaches "more", "movie", "love"). This is variant-list work, and only real transcripts can tell us which spellings matter.

**What the design should say.** Replace A1's "every time" with this, in the report, README and any demo talk:

> Listed request sentences work every time, in our tests, with the listener up or down, in Latin and Devanagari. Rephrased requests are caught less often: 0.75 on held-out synthetic lines by rules alone. Accuracy on real speech has not been measured yet. [Replace with the P12 figure once measured.]

**Three cheap things make the claim true in practice:**

1. **Teach the phrase.** The Chairperson's ritual (P4) says one line: "Agar kisi vishay par aage na badhna ho, to bas kahiye: kisi aur topic pe baat karte hain." A documented sentence that always works is an honest "every time". A real board would not announce this, so the owner chooses by ear whether it is a ritual line or a hint on the form screen.
2. **Measure on real text once.** At P12, with owner approval, he reads about 30 request lines and 10 answers, saved through `make voice-record`. Re-scoring those saved transcripts is free forever. That gives the only real recall number.
3. **Report what happened.** The session report lists each detected request and its source. A miss is then visible to the candidate, not silent.

## 5. Question 2: is the holdout clean?

**Clean as far as the repository can show, but it is not the first holdout, and the commit does not say so.**

- **Best evidence for clean:** the 8 misses are unfixed, no pattern fires on holdout lines only, there are no exact duplicates of dev lines. The misses sit just outside the tables ("muv" listed, "muve" not), which is what an unleaked set looks like.
- **Undisclosed:** the dev split contains 75 rows with ids `h001` to `h075` and the **identical shape** of the 75 `t` rows: 16/9/9/10/6/25 by kind. They are clearly an earlier holdout folded into dev, followed by a new `t` set. That is the correct procedure if the first set was spent. But it means `t` was written by an author who had just seen what failed, and `t` mirrors `h` slot by slot. All of it arrived in one commit, so git cannot show the order. **Ask the builder to state this history in the corpus notes.** The score from a first-draw set is unknown; it may have been lower.
- **Weak signals:** the garbled `t` lines are one-typo edits of dev lines (t024, t015, t016), and t043 is identical to a dev line after canonicalising. So the garbled set is not independent. Ten variant-table tokens appear only in holdout lines (`bolye`, `intrview`, `nai`, `samjh`, `subject`, and five Devanagari). Removing them all drops only 2 hits (change_topic 11/16, clarify 7/10). So at worst the bias is small, and a table with 221 of 347 entries unused by any line is broad anyway.
- **Same author.** The real limit is that one author wrote rules and holdout. That is why my blind set is so much lower.

**My 20 fresh lines.** Written and saved before scoring, scored once, no code changed. Caveat: I had read the patterns first, and a CTO picks varied shapes on purpose, so this is a stress sample, not a recall estimate. 20 lines cannot give a rate.

| Kind | Hit |
|---|---|
| change_topic | **1/8** |
| skip | 3/4 (the miss is M4: returned repeat) |
| repeat | 3/3 |
| clarify_question | 1/3 |
| end | 2/2 |
| **Total** | **10/20**, no false `end` |

Change_topic misses: "sir ye topic chhod ke kuch aur pooch lijiye", "is baare mein mujhe zyada nahi pata, kisi aur cheez pe aa jaate hain", "why don't we discuss something else", "sir agriculture pe baat kar sakte hain kya", "maybe we could shift to a different area", "tourism pe bahut ho gaya sir, ab kuch aur", "ab is topic ko yahin rehne dete hain". Clarify misses: "sir thoda clear nahi hua question", "aap kis sandarbh mein puch rahe hain".

**Owner-shaped set** (48 lines close to what he would say, unpunctuated ASR style): change_topic 21/33. The 12 misses: 7 are M1, 3 are English in Devanagari, 1 is "se hat ke", 1 is the "baad" slip. Variants of the exact sentence with no M1 noun did well, including "ala wa", "our topik", "bat karte hai".

**Other recall gaps found:**

- "i dont know sir next question", "pata nahi sir agla sawal", "sorry sir i have no idea next question": **all miss.** `skip_next_bare` is anchored to the turn start and `skip_next_tail` requires "please". After an honest "I don't know", this is the most natural skip in a UPSC room.
- "interview yahi khatam kar dijiye sir thank you" and "i want to end this interview now thank you" miss, because `_END` has no thank-you words.

## 6. Questions 3 and 4: false positives and priority

**Adversarial results beyond section 3:**

| Line | Fired | Severity |
|---|---|---|
| "can we move on from coal by 2050", "can we move on from the colonial mindset" | change_topic; `change_en_modal` has no `_END` | Should-fix; also fires as a tail of a long answer |
| "we can change the topic of the debate in parliament" | change_topic; `change_en_change` has no `_END` | Should-fix |
| "kuch aur baat karte hain kisanon ki", "kisi aur tarah ki baat karte hain sir", "dusri baat karte hain ki gaon mein paani nahi hai", "koi aur upay par baat karte hain" | change_topic; the `{0,4}` gap in `change_other_baat` | Should-fix: allow only topic nouns or at most 2 gap words, and block a following `ki` |
| "enough of this politics", "enough on paper", "I do not want to talk about my success" | change_topic | Low |
| "dalit samaj nahi aaya" | clarify; `samaj` is a variant of `samajh` | Low; clarify is cheap and not counted |
| "I don't understand it", "I am confused", "matlab", "sawal puchiye" | clarify or repeat | Acceptable for short turns |

**Devanagari and unpunctuated ASR.** Hindi in Devanagari behaves like its Latin form, including M3. Two structural concerns:

- **English written in Devanagari is not covered.** "कैन वी मूव ऑन", "लेट्स टॉक अबाउट समथिंग एल्स", "चेंज द टॉपिक प्लीज", "स्किप दिस क्वेश्चन", "व्हाट डू यू मीन" and "आई वांट टू एंड द इंटरव्यू" all miss. The corpus `devanagari` lines are almost all Hindi. Whether Prisma writes English this way under `hi-IN` is `# UNVERIFIED:`; `make stt-ab` will show. If the owner chooses `hi-IN`, this becomes must-fix. It needs about 20 variant entries. Note the collision: "आई" already means `aaya`, so "आई वांट" needs a two-word rule, not a table entry. "एंड" also means "and".
- **The context guards lean on punctuation.** `test_punctuation_ends_the_context_window` passes only because of a full stop. Whether Prisma `verbatim` punctuates is unknown here. The guards should fail towards "request" for strong patterns (M1) and towards "not a request" for weak ones (M3).

**Question 4: priority.** Current order: end, skip, change_topic, repeat, clarify.

- **`end` first: right**, because it is the only kind the candidate cannot undo, so it needs the tightest pattern and the 12-word cap. It needs to be tighter (M2). One self-repair gap: "please end the interview sorry" fires, because `sorry` is a filler. Remove `sorry` from `_END` for end patterns.
- **Repeat before clarify: right.** Repeat is cheap, and "samajh nahi aaya, phir se boliye" should repeat.
- **Skip above all change_topic: wrong.** The strong change patterns must rank above skip. Test: "is sawal ko chhodiye kisi aur topic pe baat karte hain" and "sawal chhodiye, tourism ke alawa kuch aur puchiye" both return `skip`. Skip stays in the segment, so the candidate who refused a topic gets the next tourism seed. Being wrong that way repeats Round 20. Being wrong the other way loses one seed.
- **But not every change_topic.** "can we move on, next question please" is a skip. So the order should be: **end > strong change_topic > skip > weak change_topic (`aage`, `move on`) > repeat > clarify.** Prototype result: order alone is safe once `change_x_chhod` excludes `sawal`. Without that exclusion, 5 skip lines flip (d058, d065, d067, d070, t023).
- First-match order ignores position in the turn. "next question... no wait, please repeat" gives skip. This is acceptable; do not add last-wins logic now.

**Design amendments for the orchestrator to rule on (CTO_NOTES_06 section 3 is mine, and these change it):**

1. **One-line confirmation on `end`.** The board asks "Aap interview yahin samapt karna chahte hain?" and only a clear yes closes. Anything else resumes, uncounted. This makes every remaining false `end` harmless for one extra turn. It adds a `pending_end` flag to the director in P3 and a small yes/no check. I recommend it; a real chairperson would also check.
2. **A healthy listener may veto weak rule hits.** Today a rule hit always wins. For weak patterns only (`change_aage`, `change_en_modal`, `change_x_chhod`), if the listener returned in time with `none` on a turn over 8 words, drop the hit and trace `request_disagree`. This narrows the accepted table in Ruling 42, so it needs a ruling. Strong patterns and listener failure stay as they are. Decide in P5.

## 7. Question 5: latency

Measured on this Mac, single thread, `time.perf_counter`.

| Input | Time |
|---|---|
| Corpus lines, warm | p50 0.021 ms, p99 0.066 ms, max 0.16 ms |
| 300 mixed words | 1.3 ms |
| 1,000 words | 4.3 ms |
| 5,000 / 20,000 words | 21 ms / 79 ms |
| 300 unique words, cold cache | 2.6 ms |
| Module import | about 13 ms, once |

**No catastrophic backtracking.** I ran 10 repetition attacks at 300, 1,000 and 5,000 words (`sir`, `ko`, `kisi aur`, `please`, `topic`, `can we`, `ko chhod`...). All grow linearly; the worst was 28.5 ms at 5,000 words, and a 15-minute session cap keeps a real turn far below that. Gaps are bounded and alternatives do not nest. **Safe on the hot path, on the event loop, with no thread needed.** A fix in P1.1 must not add an unbounded `\S+` gap. Add a test: 5,000 repeated tokens under 100 ms, generous enough for CI.

## 8. Should-fix, and the P1.1 chunk

**P1.1 contents (S to M, no wiring):**

1. M1 to M5, with every section 3 line added as a test.
2. Tail skip after a don't-know: allow `next sawal` or `agla sawal` as the turn's last words when not preceded by `the/a/my/this/ki/yeh`. Keep "the next question is..." clean.
3. Add `thank you`, `thanks`, `dhanyavaad`, `shukriya` to tail fillers, and `kya`, `na` after modals.
4. Widen the opener list: `why dont we`, `should we`, `we should`, `we could`, `maybe we (can|could)`, `i think we should`, `how about`.
5. Add `_END` to `change_en_modal` and `change_en_change`, and tighten the `change_other_baat` gap.
6. Add `key` to the `ke` variants. Extend avoid terms for "tourism chhodiye", "tourism nahi", "key alawa", "ala wa": today all four return an empty tuple, which also makes A1c vacuous for them.
7. **Test hygiene:**
   - Every pattern must win on at least one corpus line. 11 never do: `end_en_bare`, `skip_next_tail`, `skip_agla_tail`, `change_x_chhod`, `change_agla_topic`, `change_en_ahead`, `change_en_bare`, `change_en_rather`, `repeat_tail`, `clarify_mean_tail`, `clarify_hi_samjhaiye`.
   - Add a `# corpus history` note covering the `h` series.
   - Add my adversarial lines as dev negatives.

**The holdout after P1.1.** Items 3 and 4 will probably fix t007, and class fixes count as spending the set. That is fine if it is said out loud. The `t` figures remain the honest P1 number, marked **spent** after P1.1. Do not add "tolk", "muve", "nex" to reach a better figure. My fresh 20 are spent too, because this note prints them. **The orchestrator, not the builder, writes a third holdout of about 40 lines** after P1.1 and scores it once. Report that number. It gets frozen by a hash test.

## 9. Question 6: the avoid-terms cross-script plan for P4

**The problem.** Candidates' terms are stored as spoken ("turism", "टूरिज्म", "पर्यटन"), but the actor may write "tourism" or "paryatan". An exact match lets the refused topic back in, which A1c forbids.

**Plan, deterministic, no model call:**

1. **Aliases first.** On `change_topic`, always add the **current segment's** label and aliases to `avoid_terms`, whatever the candidate said. The refused topic is nearly always the current one, and the agenda's own words are what the actor would use. This also solves "aage badhte hain", where `avoid_terms` is empty.
2. **Alias source.** `Segment.aliases: tuple[str, ...]` in `brain/schemas.py`, 0 to 5 forms in Latin and Devanagari. For the fallback agenda, a static reviewed file (`brain/topic_aliases.yaml`, about 30 domain topics). For `plan_agenda` (P6), an optional tool field, validated, no extra call. A missing alias is allowed, never fatal.
3. **Term key for what aliases miss.** `term_key()` in a new small module maps Devanagari to Latin with a character table, then folds coarse vowels and consonants (`ou/oo/u`, `ee/e/i`, `ph/f`, `sh/s`, `z/j/s`) and collapses doubles. **Keep vowels.** A vowel-dropping skeleton makes "tourism" equal "terrorism", which would wrongly ban internal security after a tourism refusal.
4. **Matching.** Compare per token and per phrase, never by substring. Exact key equality; edit distance 1 only for keys of 6 or more letters. For a multi-word term, also match its last content word ("rural tourism" bans "tourism").
5. **Prompt.** `<avoid_topics>` lists the spoken term plus aliases. Treat candidate words as data: cap at 3 words each and at 5 terms, letters and spaces only, so a spoken instruction cannot enter the prompt.

**Prototype.** A 30-line key gives `tourism`, `turism`, `टूरिज्म`, `टूरिज़्म` all as `turism`, with `terrorism` at `tirurism` and `टेररिज्म` at `tirism`: separated. It **fails** on `agriculture`/`एग्रीकल्चर` and `paryatan`/`पर्यटन`, and "कृषि"/"krishi" also miss. English-to-Hindi synonyms cannot work phonetically at all. That is why aliases come first and the key is only a backstop. Devanagari inherent vowels make the key unreliable for words outside the alias list, so treat it as best effort.

**Tests (P4).** A matrix of 3 topics across Latin, garbled, Devanagari transliteration and Hindi word. Negatives: a refused "tourism" must still allow "terrorism" and "tropical". `test_refused_topic_never_returns` must run with the term in a different script from the actor's line.

**Known limit.** A refused topic that is not the current segment, in a script unlike the actor's, and not in any alias list can still return. Mark it `# UNVERIFIED:` and log it in the trace.

## 10. Verify for P1.1

`make test` and `make lint` with real exit codes; both board md5s unchanged; `detect_request` still imported only by tests; the builder's report prints dev table and the (spent) `t` table; each section 3 and section 6 line tested; the 8 `t` misses named as fixed or not fixed, without targeted spellings.
