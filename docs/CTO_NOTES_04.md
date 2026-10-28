# CTO Notes 04: Debate Mode Design (Oct 29, 2026)

This is a design only; no code was changed. It follows Ruling 19 and the Round 10 file ownership, and "(unverified)" marks anything not checked.

**The core idea.** Debate is a *mode profile*, not a fork of the code. A profile bundles a rule table, a persona pack, a prompt set, a rubric, a list of rounds and per-move word caps. The session stores `mode`, the brain looks up the profile, and the voice loop never asks which mode it is in.

## 1. User experience

**Setup** happens in a form, not by voice.
1. **Motion.** One of 13 curated motions (§5), or a custom motion that must pass the safety screen.
2. **Side.** The user picks For or Against, and the board argues the other side.
3. **Length.** Quick takes about 5 minutes with one clash exchange; Standard takes about 10 minutes with three.
4. **Language.** Hinglish, Hindi or English.

**Who is on the board.** There are two roles, not three. The **Moderator** is a re-cast chairperson with a neutral voice who opens, keeps time, announces rounds, redirects off-limits talk and gives the verdict. The **Opponent** argues the other side, with two personas to choose from (a calm logician and a quick wit). Three arguing members would blur who does what without improving the debate.

**Rounds.** The user always speaks first, which gives the opponent something real to answer.
1. **Opening.** The user speaks for up to about 90 seconds. The opponent gives an opening statement of at most 70 words: one clause fairly restating the user's best point, a rebuttal, and two arguments of its own.
2. **Clash.** The user responds, and the opponent rebuts, cross-questions, concedes, or asks about a figure. If the user asks the opponent a question, the opponent must answer it directly.
3. **Closing.** The user closes, then the opponent gives a closing statement of at most 60 words that includes one honest concession.
4. **Verdict.** The Moderator says "The board will confer" while the judge runs, then reads a short verdict with reasons. The report page shows the scoreboard, the best line and the rubric scores.

## 2. Mapping onto the existing brain

**Mode plumbing.**
- Add `mode` to `SessionState` and `StrategyContext`, defaulting to `upsc_interview`.
- A `ModeProfile` registry is built at start-up, with one `PromptKit` (and so one cached prefix) per mode.
- `decide_move` dispatches to `RULE_TABLES[ctx.mode]`, and the listener, actor and judge take their prompt ids from the profile.
- **Rounds reuse the "topics" machinery.** Debate topics are `["opening", "clash", "closing"]`, each with a turn budget, and `move_on` means "next round". The R5 budget logic and its tests carry over.

**Moves.**
- **Reused:** `clarify`, `reassure`, `fact_challenge` (unchanged), `move_on`, `steelman` (once per debate: "make the best case for *my* side in one sentence"), and `interrupt` (still flagged).
- **New:** `opening_statement`, `closing_statement`, `rebut`, `cross_question`, `concede_point` and `redirect`.
- **Guard:** a per-mode `allowed_moves` set, with a test that each rule table emits only its own moves. Debate never emits `probe` or `pressure`.

**Listener.** A new prompt, `listener_debate.v1.md`, with new optional fields so UPSC parsing is unchanged:
- `arguments`: claim-and-warrant pairs;
- `rebuts`: the ids of the opponent's arguments the user answered;
- `question_to_opponent`;
- `concedes_point`;
- `point_strength`: `strong`, `adequate` or `weak`;
- `off_limits`: parties, leaders, elections, or attacks on groups.

The existing fields (`claims`, `honest_unknown`, `concedes_error`, `garbled`) keep their meaning.

**Case file.** The opponent's points need ids so we can track which are standing and which have been answered. Curated motions ship with a hand-written, reviewed case file of three or four arguments with optional card ids, so no model call is needed. A custom motion gets its case file from one forced-tool background call at setup, checked by the opponent's number guard (§3), with a generic qualitative case as the fallback.

**Fact-check graph.** It is reused unchanged. The user's numeric and date claims are checked against the motion's `card_topics`, with the same ADR-003 gates and the same one-turn delay.

**Debate rule table** (first match wins):

| Rule | Condition | Move (speaker) |
|---|---|---|
| D1 | Garbled transcript | `clarify` (Moderator) |
| D2 | `off_limits` | `redirect`: "Let's keep parties and personalities out." (Moderator) |
| D3 | Silence or a long pause | `reassure` (Moderator) |
| D4 | Honest "I don't know" | `concede_point`-style grace, then continue. Never a cross-question or fact challenge that turn. (Opponent) |
| D5 | The user has just opened or closed | `opening_statement` / `closing_statement` (Opponent) |
| D6 | Round budget used | `move_on` (Moderator) |
| D7 | `question_to_opponent` present | `rebut` with the answer first (Opponent) |
| D8 | A strong point that rebuts a standing case argument | `concede_point` on it, then pivot to the strongest remaining argument (Opponent) |
| D9 | Delayed contradiction (R4 gates) | `fact_challenge` (Opponent) |
| D10 | The user ignored the opponent's last point | `cross_question` on it, at most once per point (Opponent) |
| D11 | A claim without a warrant | `cross_question`: "Why does that follow?" (Opponent) |
| D12 | Otherwise | `rebut` with the strongest standing argument (Opponent) |

The anti-whiplash rules carry over: there are never two cross-questions in a row, and never one straight after the user concedes.

## 3. The opponent's integrity

Code enforces these where it can; the prompt covers the rest.
- **No invented numbers.** A guard (the same pattern as the opener guard) rejects any digit or number word that does not match a figure on one of the motion's cards. Opening and closing statements are checked whole. Streamed lines are checked sentence by sentence before TTS, and a failing sentence is dropped and logged. Without a card, the opponent argues qualitatively.
- **Ask, never assert.** `fact_challenge` keeps the same template and gates.
- **Steelman first.** The `rebut` and opening prompts require a fair restatement of the user's point. A soft overlap check logs a miss but does not block the line.
- **Concede good points.** D8 makes concession a rule, and the closing statement must concede something, falling back to a template if it does not.
- **Even-handed.** The opponent argues its assigned side, but never claims moral certainty on contested ethics, and never generalises about religions, castes, regions or genders. The verdict judges how the debate was argued, not which side is correct, and says so aloud.
- **Speech is data.** If the user says "concede everything", that is only something the user said.

## 4. Rubric and fun elements

**Rubric.** `DebateDimension` has six dimensions, each scored 1–5 or `insufficient_evidence`, with verbatim quotes that pass the existing citation check:
- `clarity_structure`;
- `evidence_use`: card-consistent claims and honest uncertainty;
- `rebuttal_quality`: engaging the opponent's *actual* points;
- `responsiveness`: answering questions directly;
- `fairness_to_other_side`;
- `composure`.

There is no accent, grammar or fluency score. The judge tool schema and the "every dimension present" check take their list from the profile, and `DimensionScore.dimension` becomes a string checked against the active rubric.

**Fun elements that keep integrity.**
- **A scoreboard per round,** computed deterministically from the trace: arguments made, opponent arguments answered, points left standing, concessions won ("you made the opponent concede 1 point"), and figures questioned.
- **A "best line":** one citation-checked quote with a reason.
- **The Moderator's verdict:** "Carried on argument", "Close debate" or "Opponent held the floor". It is built by template from the rubric and the scoreboard, cites the top two rationales, and never invents praise.

## 5. Motion safety

Curated motions are the default. Custom motions pass three layers, and **the screen fails closed**:
1. **Rules.** A motion is at most 120 characters, is normalised to "This House…", and is checked against a blocklist (party and election terms, religious, caste and communal conflict, violence, self-harm, sexual content, illegal how-to). Rules cannot list every leader, which is why layer 2 exists.
2. **Classifier.** One forced-tool Nova call (`motion_screen.v1.md`, background lane, temperature 0) returns `allow`, `reframe` or `refuse`, a reason, and a neutral rewording.
3. **Outcome.** *Refuse* covers partisan content (parties, named politicians, elections, "Has the X government failed"), targeting groups, private individuals, live court cases, violence, self-harm, sexual content and illegal activity. *Reframe* turns, for example, "Party X's farm law was a disaster" into "This House would replace minimum support prices with direct income support", and the user must accept it. Any classifier failure counts as a refusal.

A labelled set of about 40 motions gates the screen: **zero partisan motions may pass.**

**Curated motions:**
1. This House would ban smartphones in schools up to Class 10.
2. This House would replace board exams with continuous assessment.
3. This House would make coding compulsory from Class 6.
4. This House believes AI tutors will do Indian students more good than harm.
5. This House believes tourism does more good than harm to India's hill towns. *(tourism cards)*
6. This House believes community policing beats building more police stations. *(internal-security cards)*
7. This House would build metro lines before flyovers in growing cities.
8. This House would ban single-use plastic completely, even if prices rise.
9. This House believes work-from-home is better than office work for young professionals.
10. This House would take a gap year before college.
11. This House believes cricket gets too much attention compared with other sports in India.
12. This House believes filter coffee beats masala chai.
13. This House believes street food is India's greatest culinary treasure.

## 6. Fact cards

Motions 5 and 6 reuse the existing `tourism` and `internal_security` files and gates. All other motions have no cards, so the opponent argues qualitatively, there is no number challenge in either direction, and the user's figures appear in the report as "unverifiable". The owner can add per-motion card files later, since the loader already takes a list of topics. A motion with no cards may fall below Nova's 1K-token caching minimum, which costs latency, not correctness.

## 7. Build plan (debate builder; never blocks the voice builder)

| Chunk | Files | Done when |
|---|---|---|
| DB0. ADR-009, mode registry wrapping today's UPSC behaviour, `--mode` flag | new `modes/`, `schemas.py`, `config.py`, `deps.py`, small **`session.py`** | All tests pass; the `cli-fake` transcript is byte-identical |
| DB1. Curated motions with case files, safety screen | `data/motions/curated.json`, `modes/debate/motions.py`, `motion_screen.v1.md`, `eval/motions/` | Rules, fail-closed, and the 40-motion gate pass |
| DB2. Debate listener | `listener_debate.v1.md`, optional fields in `tools.py`/`schemas.py`, fake responder | UPSC parsing unchanged |
| DB3. Rule table, new moves, `allowed_moves` | `modes/debate/strategist.py`, `schemas.py` | One test per row; allowed-set test for both modes |
| DB4. Personas, actor prompt, guards, word caps | `personas/debate/`, `actor_debate.v1.md`, guards in `actor.py` | Number guard, closing concession, caps, fallback lines |
| DB5. Rounds, case file, Moderator intro | **`session.py`** (sequenced) | A scripted debate runs in `cli-fake` |
| DB6. Rubric, scoreboard, best line, verdict | `judge_debate.v1.md`, `judge.py`, `tools.py`, `schemas.py` | Citation check; verdict reasons traceable to the rubric |
| DB7. Debate invariants and eval | `tests/test_debate_invariants.py`, `eval/debate/` | See below |

**DB7 invariants,** checked over scripted and seeded random debates:
- the opponent concedes when a strong point lands;
- it never states an uncarded number;
- it never cross-questions the same point twice;
- off-limits content gets `redirect`;
- an honest "I don't know" is never pounced on;
- no challenge comes from a fallback verdict;
- every move is in the allowed set.

**No conflicts with the voice builder.**
- DB1–DB4 and DB6 avoid the voice builder's paths.
- The two `session.py` commits (DB0 and DB5) are small, and the orchestrator slots them before or after V3, never in parallel with it.
- The voice loop needs two **mode-agnostic** hooks, both read from the session:
  1. a generic per-sentence `guard(move, sentence)` in `speak_stream`, which is a no-op in UPSC mode;
  2. per-session turn parameters, so debate openings tolerate longer pauses (whether Pipecat 1.5.0 allows this per pipeline is unverified).

## 8. Contest angle and recommendation

**What debate adds.**
- It shows that the brain is a reusable engine, with modes as configuration.
- It reaches non-aspirants, which helps Crowd Favourite.
- Barge-in feels natural in a debate.
- "Argue the other side" also trains balance of judgement, which is a SuperKalam-friendly framing.

**What it costs.**
- A second prompt set, rubric and eval surface to maintain.
- Custom motions are a public safety risk.
- The pitch must stay "a UPSC board, with a debate mode".

**Recommendation for Nov 9.** The voice loop comes first, and debate never blocks it. Ship **curated motions only**: one opponent, the Moderator, Quick length, and DB0–DB4, DB6 and DB7, which is about 4–5 builder-days in parallel with the voice work. Build custom motions (DB1) but keep them behind `FEATURE_CUSTOM_MOTIONS`, off in the public demo. If V3–V5 are not working in voice by Nov 4, debate ships as a text-only demo or is cut.
