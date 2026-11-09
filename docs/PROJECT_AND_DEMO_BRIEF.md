# Mock Board: project and demo brief

**Updated:** November 6, 2026  
**Primary Gnani award story:** Jugaad Genius  
**User:** UPSC aspirants practising the Personality Test  
**Product framing:** a voice-first interview mentor, not an official UPSC assessment

## The user problem

UPSC aspirants can practise written answers and ask study questions, but the Personality Test asks them to think and speak under follow-up. Mock Board gives them a realistic oral rehearsal with a five-member panel that remembers the answer, handles requests to repeat or change topic, and asks a follow-up based on the candidate's reasoning. After the sitting, it shows evidence-backed feedback and a concrete practice plan.

This complements SuperKalam's public positioning around an AI mentor, answer evaluation and daily discipline: Mock Board focuses on spoken Personality Test practice. This is product positioning only; it does not imply a partnership or an integration.

## The unexpected use of Gnani

Use Gnani speech models as the input and output of an adaptive oral assessment loop:

1. **Prisma** turns the candidate's speech into live transcript segments.
2. **The listener** extracts the candidate's point, reasons, claims and requests from each segment.
3. **The director** uses the answer and interview memory to choose a follow-up, a repair question or a topic hand-off.
4. **Timbre** speaks the panel's next line, with a distinct role for each member.
5. **The report** ties feedback to quotes from the candidate's own answers and suggests what to practise next.

The product's Jugaad angle is the complete speech-to-coaching loop: the speech model's output changes how an examiner thinks and responds, and the next spoken turn is adapted to that candidate. Tanglish is an especially visible demonstration of this loop, but it is not the only intelligence claim.

## What is implemented

- Five fictional panel members, form-based interview agenda, member hand-offs and per-session memory.
- Structured listening for stance, reasons, claims, unclear speech, and candidate requests.
- A deterministic director with bounded interview moves. A substantive answer gets a follow-up when the remaining budget allows it.
- Numeric fact challenges are delayed until checks agree and need a source card; the board asks about the difference instead of asserting that the candidate is wrong.
- Actor rules constrain length, open-ended questions and unsupported factual claims.
- The report uses UPSC personal-quality dimensions plus two clearly separate practice indicators. Scores require evidence; the rubric does not score accent, dialect, grammar or fluency.
- Tanglish is available in the form and routes Prisma with `ta-IN`. Tamil fallback questions, panel lines and prompt versions are present.
- The voice report now exposes dimensions, rationales, evidence quotes, questions asked and practice prompts. The form shows language-matched topic-change guidance.

## November 6 update: what changed, what was measured

All of this ran offline (fake model, fake speech). **No Gnani or Bedrock call was made**, so nothing
below is a claim about real speech or Nova's behaviour.

**An independent "aspirant" review** (a separate agent playing a Hindi-medium aspirant, an English-medium
engineer, a nervous first-timer and a Tanglish speaker, offline only) reported 16 reproducible findings.
The ones fixed here:

| Finding | Fix |
| --- | --- |
| No Tamil-script or Tanglish request was recognised by rules; a Tanglish candidate could not end by voice, and the form's own Tamil hint phrase did nothing | `brain/requests_ta.py`, an additive Tamil/Tanglish phrase layer the director consults after the frozen detector (`requests.py` is untouched, Ruling 57). Tamil yes/no for the end confirmation. |
| The Hindi hint ("कृपया विषय बदलें।") was not recognised | Hints now name phrases the rules recognise, for change topic and repeat, in all four languages; `tests/test_request_hints.py` parses `app.js` and fails if a hint stops working. |
| "Yes sir, please end it" to "end here?" resumed the interview | A reply that opens with yes and adds only end or thanks words closes; anything else still resumes. |
| A woman member said "main theek se sun nahi paaya" (masculine) | Panel clarify and fallback lines in Hindi and Hinglish are gender-neutral. |
| An honest "I don't know" was ranked the weakest moment | It no longer adds weakness by itself; length and one-sidedness still count. |
| The practice question began with "Zaroor, prashna phir se suniye." | Fixed lines are stripped from the weakest question and the replay. |
| The practice plan was the same three lines for everyone | One place is kept for the segment the candidate found hardest. |
| Request turns leaked into member notes ("spoke about please, repeat") | Request turns add no note. |
| Tamil text: "time is over" at the answer cap (Ruling 49), "அரபு" (Arab) for *arab*, case endings glued to names | Corrected; still needs a fluent speaker. |
| Captions in Tamil were read by an English screen-reader voice; 1px focus ring; "End interview" had no confirmation | `lang="ta"` on Tamil captions, 2px focus ring, a confirm before ending by tap. |
| Privacy: nothing said the server keeps a text record, or that text goes to Bedrock in Sydney; "B.Tech from IIT Kanpur" was spoken aloud | A privacy note on the home page; a form value that names a college or "from" somewhere is never spoken, and a planned seed naming one is refused. |

Found while mapping the code (not by the reviewer): English, Hindi and Tanglish sittings heard the
**Hinglish** fallback template whenever the model line failed or a guard cut it. Each language now has
its own fallback lines (`actor.filler_for`).

**New: "How the board heard you"** in the report (page, CLI and the saved JSON). For every answer: the
transcript as recognised, the listener's own-words note (a note that copies the transcript is dropped),
any request with how it was found (phrase rules or the model), and what the board did next. It says
plainly that a mismatch is the tool's limit, not the candidate's. Built by code from session state;
the judge does not write it. Checked rendered in the browser on `voice-fake` at desktop and 375px width
(no horizontal scroll), using injected report data, not a real sitting.

**Measured, offline, synthetic.** `make eval-requests` (new): 49 labelled lines we wrote, across English,
Hinglish, Hindi, Tamil script and romanised Tanglish, positive, negative, ambiguous and adversarial
(prompt injection, reported speech, negation, self-correction, names, figures, fillers). Rules alone:
**23/29 requests recognised, 0/20 false actions.** The six misses are rephrasings the frozen detector
does not cover (for example "Sir, kya aap sawal dobara bol sakte hain?", "இன்னொரு தடவை"); in a real
sitting the listener's vote is the second chance for those, and that has not been measured. The Tamil
rows were written in the same session as the Tamil phrase list, so they are a design check, not a holdout.
Sample size is tiny; this is a regression guard, not an accuracy figure.

**Not changed, needs the owner:** "yaar" still draws the register line (Ruling 79), including in "yaar
dost" (friends) and just before a reassurance; a topic change in the last segment still closes the sitting
(the reviewer found this abrupt; it is a locked design); the report is English even for a Hindi sitting;
common rephrased requests ("thoda dheere boliye", "ek minute sochne dijiye") have no move. The fake
stand-ins still answer English candidates in Hinglish; that is the offline actor, not the product.

## November 6, later: one real run on generated Tanglish speech (owner approved)

**What ran.** Nine scripted Tanglish answers (ours: a local problem, a negation, a self-correction, a
name, an honest "not sure, but…", and repeat, topic-change and end requests) were spoken by **Timbre**
(`timbre-v2.5`, `ta-IN`, voice Asmita) and transcribed by **Prisma** (`prisma-v2.5`, `ta-IN`). The
transcripts then drove one **Nova Pro** panel sitting (text mode, `--max-answers 8`). This is
**generated speech, not a human speaker**; it says nothing about accents or real code-switching.
Trace `traces/tanglish-live-1.jsonl` (gitignored); `make check-trace` PASS. The WAVs were deleted.

**Spent:** 774 Timbre characters; about 59 s of Prisma audio (9 streams, with trailing silence);
Nova about 46k input and 3.7k output tokens over the turn and judge calls (about US$0.05 at the US
list price; the Sydney price was not confirmed). Roughly ₹7 in all at the project's own estimates.

**Measured (n = 9 clips, one sitting):**
- Prisma last audio → final transcript: 451–1,128 ms, median 680 ms.
- English words came back in Tamil script ("Madurai" → "மதுரை", "PPP model" → "பிபிபி மாடல்"). One
  meaning-changing error: "வேறு" (other) → "வேர்" (root), in the topic-change request.
- Requests on Prisma's text: the phrase rules caught 2 of 3 (repeat, end); the listener caught the
  topic change the rules missed, so all three were handled. **One false action:** the listener voted
  the honest "எனக்கு சரியாக தெரியாது சார் ஆனால்…" ("not sure, sir, but…") answer as `repeat`, and the
  board asked the same question again instead of taking the answer.
- Board lines were all in Tamil script. One line was cut mid-word ("அப்படி என்ற") by a Latin-sized
  token budget. Two lines restated the candidate's own words, which Ruling 37 forbids; the copy
  guard does not see Tamil inflections.
- Text-mode timings: listener 1.0–2.9 s per turn; end of turn → first actor token 2.1–2.8 s on warm
  turns (4.2 s on the first).
- The listener also marked the *next* answer as "I don't know" with no note, apparently carrying the
  previous turn over. That is a model behaviour and is not fixed here.

**Fixed from this run, each with a regression test built from Prisma's real output**
(`tests/test_live_tanglish_findings.py`, and 8 `pg-*` rows in `eval/requests/multilingual.jsonl`):
a model-only repeat, clarify or skip is not acted on when the turn has an honest "I don't know" and 7
or more words (the answer that follows it is taken); Tamil and Hindi lines get 4× and 3× the token
budget so they finish (the word cap still sets length); "வேர்" before a topic word and a verb counts
as "வேறு"; the report describes the end confirmation correctly. **Not fixed:** the Tamil restating
(needs an actor prompt version and a Tamil-aware copy check, then a Nova run), and the listener's
carry-over.

**The Hinglish human sitting was not run**: it needs the owner at the microphone.

## Upgrade #2 built: microphone and language check

On the form, the candidate reads one short neutral line in the language they chose
(`speech/mic_check.py`). The server sends it to Prisma with **the same locale the sitting will use**
(one shared `lang_code_for`), and the page shows what the board heard, the words it missed, and plain
advice (clear / partly / not understood / no speech / check not available). Nothing is stored. Silence
is answered without a Prisma call. A real check is counted through access admission like other paid
utilities (polite message at the cap; the interview is not affected). Audio is capped at 10 s.
Tested offline against the project's fake Prisma server and the fake-speech app
(`tests/voice/test_mic_check.py`, 20 tests), and seen rendered in the browser with the demo result.
**Not yet run against real Prisma**; one check costs about 8 seconds of Prisma.

## Upgrade #3 built: a spoken second attempt at the weakest question

In the report, "Practise this moment" now lets the candidate try again. The same member asks the
question in their own voice (**Timbre**, one request, the member's panel voice). The candidate
answers aloud and **Prisma** hears them live, over a WebSocket relay that streams the microphone
while they speak, so the transcript is ready about a second after they stop. They can also type
instead (no microphone, or the attempts are used up). The page then compares the two answers on
five parts it can actually observe: a reason, who is affected, a first step, another side, a cost
or limit, plus length. It shows the transcript and says what was added and what to try next. It
gives **no score** and runs in code with no model call (`brain/second_attempt.py`). The cue words
are ours, in English, romanised Hindi, Devanagari and Tamil, and are unmeasured on real speech; the
page says the checks look for words like "because" and "first".

One attempt is one access count (real speech only), with at most one spoken question and one live
listen. Nothing is stored. Tested offline against the project's fake Timbre and fake Prisma servers
and the demo app (`tests/voice/test_second_attempt.py`, 21 tests), and seen working in the browser
in demo mode (typed answer, and the demo relay). **Not yet run on real Timbre or Prisma, and not with
a person speaking**: one attempt costs one short Timbre line and up to 90 s of Prisma.

## November 6: the owner's own spoken check (real Prisma and Timbre, his voice)

The owner ran the microphone check and one spoken second attempt in Chrome on his Mac (real
Prisma `hi-IN` and Timbre `timbre-v2.5`, fake model). **Human speech, one speaker, n = 1 answer.**
What it showed, and what was done:

- **Hinglish came back in Devanagari** (his `.env` sets `STT_LANG_CODE=hi-IN`), so the check compared
  Devanagari with the Latin line and looked like a miss. The check now accepts either script and says
  which one the board wrote.
- **The transcript of a 109-word Hinglish answer read back accurately to him**, including English words
  in Devanagari ("यूज", "डिसीजन"). Example of a mis-hearing: he said "Claude" and Prisma wrote
  "क्राउड" (crowd), next to "चैट जीपीटी".
- **Timbre read "use" (उसे, "it") as the English "use"** in "aap use kaise sambhalenge".

Fixes, both offline-tested (`tests/test_heard_repair_and_tts_text.py`, `tests/voice/test_second_attempt.py`):
- `speech/tts_text.py`: for Hinglish sittings, a few romanised words that share a spelling with English
  (use, is, main, do, the, hi) are written in Devanagari **in the text sent to Timbre only**, when a
  neighbouring word shows the Hindi sense; "AI ka use" stays English. Captions are unchanged. **Owner's
  ear, Nov 6:** with the fix, Timbre (`timbre-v2.5`, `auto`, voice Poorvi) read "aap उसे kaise
  sambhalenge" correctly (one Timbre line). The other five words (is, main, do, the, hi) have not
  been heard yet.
- `brain/heard_repair.py` + `prompts/repair.v1.md`: one Nova call lists at most three likely-misheard
  words per answer with what was most likely said. Code keeps a pair only if the heard words are really
  in the transcript, both sides are short, and neither touches a number or a negation. It is shown as
  "'क्राउड' was probably 'Claude'" beside the transcript, in "How the board heard you" and in the second
  attempt; the transcript is never edited. **One Nova run (owner-approved, Nov 6)** on the owner's
  own 109-word transcript: 1 call, 1,023 input and 168 output tokens, 3.6 s. Nova proposed three
  pairs and code kept all three: "क्राउड" → "Claude" (right); "चैट जीपीटी" → "ChatGPT" (not a
  mishearing, the same word in another script: noise); "ए आई टी सी" → "AITC" (wrong; from the
  context the owner most likely said something like "AI ki training", and Nova guessed an
  abbreviation). So repair.v1 catches the case it was built for but also shows script changes
  and confident guesses. **Follow-up, same day (owner-approved):** code now refuses a pair whose
  heard words only spell letters the suggestion already contains (`is_script_change`: "ए आई टी सी"
  -> "AITC", "जीपीटी" -> "ChatGPT"), so v1's three pairs reduce to "Claude" alone. A `repair.v2`
  prompt (read-aloud, sense and sure tests) ran once on the same transcript (1,175 in / 197 out
  tokens; a first attempt failed with a Bedrock validation error, 0 tokens) and did **worse**: it
  missed "Claude", suggested "डिसीजन" -> "decision" (a script change the code does not catch), and
  made one good catch ("खा सकते" -> "चल सकते", "eat" -> "work") that code dropped because the pair
  included "नहीं". v2 is kept on file, not adopted; the default stays v1 plus the code check. This
  is one transcript: a small labelled set of real transcripts is needed before calling it reliable.

## Five upgrades, ranked

Ranked by user impact, confidence, feasibility before Nov 9 and Gnani value. None duplicates a current
feature (likely questions, text replay and the report by member already exist).

1. **"How the board heard you", then a live "that's not what I said" (built: report part; on its first
   real run it exposed a false repeat, a cut line and restated words within minutes).**
   *Problem:* code-switching and accented candidates cannot tell whether a weak follow-up came from their
   answer or from a misheard transcript, so they distrust the feedback. *Experience:* after the sitting,
   each answer shows Heard / Understood / Request / Next. Next step: a button on each live caption that
   asks the board, uncounted, to hear it again. *Why UPSC:* the board's follow-up is only fair if it
   follows the candidate's meaning; seeing the gap teaches clearer speech without judging accent.
   *Gnani:* makes the Prisma → listener → director loop visible: the creative use is the transcript
   steering the panel, shown honestly. *Cost/latency:* none (data already in the session). *Privacy:* the
   transcript was already in the report; nothing new is stored. *Failure:* a missing note shows nothing
   rather than a guess. *Metric:* share of rows a candidate marks "not what I said", per language; misheard
   rate before and after a locale change. *Risk:* exposes errors in the demo; that is the point.
2. **A 15-second mic and language check before the sitting (built Nov 6; real Prisma check pending).** The Chairperson (Timbre) reads a
   short line; the candidate reads it back; Prisma's transcript is shown with matched words, and the page
   suggests `hi-IN`, `en-IN` or `ta-IN`, flags a silent mic or echo, and says to use headphones. *Why:*
   every later turn depends on transcript quality; today a wrong locale fails silently. *Gnani:* Prisma as
   a calibration instrument, Timbre as the familiar voice. *Cost:* one short STT stream and one TTS line per
   sitting. *Metric:* garbled-turn rate and clarify count per sitting with and without the check. *Blocked
   on* a real Prisma run (owner approval) to set thresholds honestly.
3. **A spoken second attempt at the weakest question, compared (built Nov 6; real run pending).** The same member's voice asks
   it again; the listener reads both answers; code compares only observable things (a reason given, the
   other side named, a first step stated, length) and never invents a score. Extends the text replay
   (Ruling 48's pluggable interface). *Cost:* one actor-free turn (seed is fixed), one listener call.
   *Metric:* share of second attempts that add a reason or a first step. *Effort:* a voice route and page
   state; about a day.
4. **A spoken debrief in the candidate's language (not built).** The Chairperson says one strength with
   the candidate's own words, one practice item and the next step, in Hinglish, Hindi, Tamil or English.
   Helps low-vision and low-literacy users and fixes the English-only report for Hindi sittings. *Cost:*
   about 60 words of Timbre per sitting. *Risk:* a translated report needs fluent review per language.
5. **Private progress across sittings, no account (not built).** The page keeps practice items and
   per-dimension evidence counts in the browser only, and opens the next sitting with "last time you
   practised…". *Gnani value:* low; *user value:* repeat practice. *Privacy:* nothing new on the server.

**Built first: #1**, because it costs nothing per session, uses data the board already has, can be tested
fully offline, and answers the trust question every code-switching candidate asks ("did it understand
me?"). It also shows judges the speech-to-coaching loop without overstating it.

## AI behavior to show and measure

Show behavior rather than model names alone. Collect a small, labelled test set and report the measured results honestly.

| Behavior | What to measure |
| --- | --- |
| Speech understanding | Preservation of the answer's point, negation, numbers, names and code-switches |
| Request handling | Recall for repeat, topic change, skip and end requests; false actions on ordinary answers |
| Adaptive questioning | Human rating of whether the follow-up uses something the candidate actually said |
| Recovery | Whether unclear or incomplete speech prompts for repair instead of an invented interpretation |
| Grounded feedback | Quote verification for scored dimensions; false factual corrections on the synthetic eval set |
| Voice responsiveness | End-of-speech to first board audio, reported as p50/p95 only after enough real runs |
| Spoken quality | Listener ratings for clarity, naturalness and respectful register, by language |

**Live voice-path smoke test (November 4, 2026):** one short provider round-trip used Timbre-generated Tamil speech as input, Prisma v2.5 with `ta-IN`, one real Nova board turn and report, and Timbre output. Prisma returned four final Tamil-script segments; the concatenated transcript preserved the stated local drinking-water problem, proposed coordination with local government, and the contrast about groundwater protection. The board's Tamil follow-up referred to the candidate's long-term-solution point; neither the actor nor report fell back. The report citation check passed. The board line was then synthesized with both the app's default Chairperson voice (`Vikrant`, `auto`) and a Tamil voice (`Asmita`, `ta-IN`); Prisma recovered the same follow-up wording from both. Final-transcript latency after the last audio frame was about 0.5 seconds on these short generated clips.

This is a controlled integration smoke test, not human-speech or code-switching validation and not a benchmark: the candidate audio was generated by Timbre in Tamil, there were only three short STT runs, and a fluent Tamil listener has not rated naturalness, pronunciation or register. The generated audio and raw transcript logs were removed after checking the results.

## Tanglish validation owned by the project

The owner does not speak Tamil. The project can still own this work: write and maintain the Tamil/Tanglish interface copy and test prompts, then ask a fluent Tamil speaker to review pronunciation, register and meaning. The speaker should test ordinary code-switching, a negation, a number, a name, and a request to repeat or move on. Capture transcripts and outcomes, not microphone audio, unless a speaker separately agrees to recording. Keep a tested phrase log so regressions are repeatable.

## 60-second demo shape

1. **0–8 seconds:** State the problem: written-answer practice does not rehearse a live interview with follow-up.
2. **8–20 seconds:** Show the Tanglish interview selection and one natural spoken answer. Use a consenting Tamil speaker; use fictional form data.
3. **20–35 seconds:** Show the board's follow-up using a detail from that answer. If it misses a phrase, show the repair question rather than hiding the failure.
4. **35–50 seconds:** Show the report with one score, its verbatim evidence, and the next practice prompt.
5. **50–60 seconds:** Name Prisma and Timbre, explain what each did, and close with the user benefit.

Only use the success path after it has passed a real, understandable spoken answer. Do not edit the video to imply unmeasured accuracy or a SuperKalam/Gnani partnership.

## November 9: final hardening (owner-approved paid checks)

Six real Nova sittings and a real Gnani round trip, each read line by line. What they found, and what changed:

- **English sittings were half-deaf.** Prisma on `hi-IN` writes English in Devanagari, so "Could you repeat the question?" was never recognised. English now uses `en-IN` (all five English requests recognised live), Tamil `ta-IN`, Hinglish and Hindi `hi-IN`, in code rather than in `.env`.
- **The repair step's "validation errors" were Nova breaking its own tool call.** One retry with a short correction fixes it (live: attempt 2 succeeded).
- **Mitra lost her own questions.** A restatement that reused the speaker's words used to stop the whole line; now only that sentence is dropped. `partner_actor.v2` keeps recaps in the session's language.
- **The study partner now has the board's controls:** repeat, leave this point, think, and end with one spoken check.
- **Throttling:** a scripted run (a turn every few seconds) hits Bedrock's limit; sessions survive on fixed lines, and the notes and repair calls now retry with backoff.

Numbers (generated speech, not a human voice): Prisma's final transcript arrived 0.2–0.9 s after the audio ended; the Hindi line came back exactly, Tamil 0.90 similar. Nova today ≈ US$0.14.

## Award and submission plan

Lead with **Jugaad Genius**: the adaptive voice-to-coaching loop is the creative use of Gnani. If the participant is entering alone, describe the end-to-end solo work as execution context for **Solo Ninja**. Gnani's rules allow a participant to win at most one award, so this is one entry with one primary story.

The final submission still needs the working public demo video, project explanation, Gnani models/APIs used, contribution details, and nominated public post link. The official deadline is November 10, 2026 at 11:59 PM IST. The demo post must be authentic and public; the challenge page says verified post engagement is used for shortlisting. See [Gnani's challenge page](https://www.gnani.ai/internship).

## Next engineering steps

(Updated November 6; the earlier list is kept below it.)

1. With the owner's approval: one short real Hinglish and one Tanglish sitting, then read the trace and
   the new "How the board heard you" block to measure misheard answers and real request recall.
2. A fluent Tamil speaker reviews `requests_ta.py`, the Tamil lines and the hint, and says the request
   phrases they would really use.
3. Owner decisions: "yaar" (Ruling 79), last-segment topic change, report language for Hindi sittings.
4. Upgrade #2 (mic and language check) once a real Prisma run sets its thresholds.

Earlier list (November 4):


1. Review the visible report and Tanglish topic-change hint in the browser.
2. Run a fluent-speaker Tanglish session and capture the transcript, intent decisions and timings without recording audio by default.
3. Fix any meaning-changing recognition or request-routing failures; repeat the small test set.
4. Listen to Tamil output with a fluent speaker and choose voices based on that review.
5. Record the demo, publish the public post and complete the submission checklist before the deadline.
