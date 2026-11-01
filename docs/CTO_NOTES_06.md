# CTO Notes 06: Panel Phase Design (Nov 1, 2026)

**Scope and method.** This is a design note at HEAD `00f653d`. No source code was edited.
- **Commands run (all offline).** `pytest` with the voice extra installed (821 passed), `ruff` (clean), `cli --fake` for UPSC and debate, and a small local LangGraph timing script.
- **Read.** The brief, rounds 16-23, Rulings 5/25/28-29/34-39, ADR-009, the code listed in the task, and the installed `langgraph` 1.2.17, `langgraph-checkpoint` 4.3.0, `pipecat-ai` 1.5.0 and `pipecat-gnani` 0.5.12 sources.
- **Docs read.** LangChain's persistence and subgraph pages.
- **Real calls.** None. No Gnani or Bedrock call was made for this note.
- **Labels.** `# UNVERIFIED:` marks anything neither the code nor primary docs settle.

## 0. Standing constraint on every chunk (owner's rule, Nov 1, 2026)

**No real Gnani or Bedrock call that costs credits without the owner's approval.** This overrides the CLAUDE.md line saying real-service checks can run on this Mac.
- Build and verify with installed packages, code, primary docs, the fake LLM and fake speech.
- **A builder step that needs a real call stops and asks through the orchestrator.** It does not run the call "small". This covers `make cli-real`, `voice-upsc`, `voice-debate`, `voice-record`, `stt-ab`, `voice-lab`, `eval.debate.runner --real`, and everything under `probes/` that reaches a service.
- Free targets stay allowed: `make test`, `lint`, `cli-fake*`, `voice-fake`, `voice-lab-fake`, `eval*` (fake), `stt-ab --dry-run`.
- Anything that depends on a paid check stays `# UNVERIFIED:` and goes on the list in §13.

## 1. What the candidate experiences

1. They fill in the form (the page, a CLI prompt, or `--form-file`). They can press "Likely questions" first.
2. The Chairperson says a fixed ritual line ("Namaste. Please be seated."), then asks one form-based question.
3. The Chairperson hands over aloud: "Dhanyavaad. Smt. Menon, please." The next voice takes up the candidate's home district, and later questions can refer to what they said earlier.
4. "Tourism ke alawa kisi aur topic pe baat karte hain" gets an acknowledgement and a hand-off to the next member, every time, and tourism does not come back.
5. "Please repeat" repeats the question. "Matlab?" rephrases it. "I'd rather not attempt this" moves to another question.
6. Five segments, about 15 answers, about 15-18 minutes, ending with "Thank you, that will be all."
7. The report is grouped by member, with the weakest question, a structure-only outline, a short practice plan, and "replay this moment".

## 2. Decision 1: the director graph and the Session API

**One `ainvoke` per candidate turn, inside `end_of_turn`, on a model-free graph, with `InMemorySaver`.**

```
START -> commit_last -> understand_turn -> route --> handle_request -+
                                                 --> handoff --------+--> END
                                                 --> next_question --+
                                                 --> close ----------+
```

Every node is pure code. **No node calls a model, so the graph cannot add model latency.** A test runs it with an LLM client that raises on any call.

| Node | Does |
|---|---|
| `commit_last` | Folds the previous finished turn (queued by `hooks.apply`) into memory and the request log. |
| `understand_turn` | Takes the merged `Analysis` and raw transcript, runs `detect_request`, resolves disagreement (§3). |
| `route` (edge) | Priority: `end` → close; wrap-up due → close; request → `handle_request`; base move is `move_on` → `handoff` (or `close` if none left); else `next_question`. |
| `handle_request` | Builds the plan for repeat, rephrase, skip or change_topic. |
| `handoff` | Picks the next segment, seed and Chairperson preface. |
| `next_question` | Keeps the strategist's move. Upgrades a second R8 `probe` on one seed to the next seed. |
| `close` | Fixed closing line; sets `finished`. |

**State (`DirectorState`).**
- `agenda: Agenda` is replaced whole.
- `ctx: StrategyContext` is written on each invoke and overwritten. It carries the segment index and questions used, so **`SessionState` stays the only owner of position**.
- `memory: PanelMemory`, `requests` (append-only) and `decisions` (append-only, for audit).
- Per-invoke inputs: `base_move`, `analysis`, `raw_text`, `completed_turn`, `wrap_up_due`.
- Output: `decision: PanelDecision` (final `BoardMove` plus `PanelContext` for the actor).

**Why this shape.**
- The session already does segment analysis during speech, so the move is precomputed (Ruling 9). The director is a last, instant step on top, not a replacement.
- `end_of_turn` builds `base_move` exactly as today, including deadline fallback, silence and turn cap. Then it calls a new optional hook, `hooks.decide_turn(turn, base_move, analysis)`. Debate has no such hook, so its path is untouched.
- **Reopened turns (Ruling 31) are safe.** The decide path writes only `decision`, which the next run overwrites. Memory changes only in `commit_last`, for turns `apply` really finished. No reopen event is needed.
- **`_finalize` and `apply` are sync and `ainvoke` is async.** That is why the commit is deferred to the start of the next invoke. `hooks.report()` runs one last invoke so the final turn is folded too.
- **Failure.** `decide_turn` catches any graph exception, calls the same pure function `decide_panel(...)` directly, and records `director_error`. A request is honoured even if the graph breaks.

**Checkpointer: `InMemorySaver(serde=make_serde())`, `thread_id = session_id`.**
- Verified: the class is in `langgraph-checkpoint` 4.3.0 and has `adelete_thread`. `langgraph-checkpoint-sqlite` is **not installed**. LangChain's page says in-memory checkpoints are lost on restart, and calls SqliteSaver local development storage.
- A crashed voice session cannot resume anyway because its WebRTC call is gone, so durability buys nothing here. It lasts the session, which is what the brief asks.
- New Pydantic types must live in `brain/schemas.py` or `_serde_types()` will not allow-list them.
- `hooks.report()` deletes the thread.
- Measured locally (toy graph, 2 nodes, 20 KB input, 40 invokes, this Mac, no network): p50 0.76 ms, max 2.29 ms. Real figures come from the budget test in §10.

**Fast and fact-check graphs are not subgraphs (see D1).** The fast graph runs per segment, during speech, on its own queue. Fact checks must never be awaited. Putting either inside a superstep would make a turn wait on them. Docs also warn that parallel calls to per-thread subgraphs can conflict.

**Agenda planning: a separate stateless graph, `START -> plan_agenda -> validate_agenda -> END`, on the background lane.** `"plan_agenda"` is not in `LANE_BY_CALL`, so it lands on the background lane automatically.

- **The deterministic agenda comes first and always exists.** `build_fallback_agenda(form)` runs synchronously in the hooks' constructor, so `topics` exist from the start. Nova only upgrades seed text. Segment ids never change.
- **Timing.** `session.opening_ritual()` starts the plan task and returns the ritual line. The voice loop speaks it (a few seconds of natural cover), then calls `open()`.
- `open()` waits at most `agenda_plan_wait_s` (default 1.5 s) for what is left. The CLI gets the same bound, so fake runs stay deterministic.
- **If slow:** the interview starts on the fallback; a late plan upgrades only segments not started yet (`agenda_upgraded` event).
- **If it fails:** the fallback stays (`agenda_source: "template"`). Nova failure cannot stop an interview.
- `validate_agenda` replaces each bad seed one by one with its template.
- `# UNVERIFIED:` Nova's time for this call. The wait bound is a guess until an approved real run.

## 3. Decision 2: candidate requests

**Both, and the deterministic detector wins.** The listener field adds no extra call.

**Detector: `brain/requests.py`, `detect_request(text) -> RequestHit(kind, avoid_terms, pattern_id)`.** It runs on the **turn's joined raw transcript**, so it works when the listener failed, timed out or called the turn garbled.
1. NFC normalise, lowercase, strip punctuation.
2. Canonicalise tokens from a variant table in both scripts: `alawa/alaawa/ilawa/alava/allawa/अलावा → alawa`, `topic/topik/टॉपिक/vishay/विषय → topic`.
3. For tokens of 5 or more letters not in the table, allow edit distance 1 to the key words (stdlib only).
4. Match phrase patterns.
5. **Position rule:** a hit counts if the answer is at most 20 words, or the phrase is in the last 12 words. A long answer that mentions "something else" midway is not a request.

"ke alawa" alone never fires. It needs a move word (`kisi aur`, `dusra`, `baat karte`, `chhodiye`, `aage badh`, `something else`, `move on`). So "tourism ke alawa agriculture bhi important hai" is not a request.

| Kind | Examples | Action |
|---|---|---|
| `change_topic` | "kisi aur topic pe baat karte hain", "can we move on", "let's talk about something else" | Ack + hand-off; closes if last segment |
| `skip` | "next question please", "I'd rather not attempt this", "is sawal ko chhod dete hain" | Ack + next seed; hand-off if none left |
| `repeat` | "please repeat", "phir se boliye", "फिर से बताइए" | Repeats last question; no model call; not counted in budget |
| `clarify_question` | "what do you mean", "sawal samajh nahi aaya", "matlab?" | Actor rephrases; verbatim repeat on failure; not counted |
| `end` | "I want to end the interview", "interview yahin khatam karte hain" | Chairperson close; requires 12 words or fewer |

**Listener field (`listener.v4`).** `candidate_request` enum, in a **new** `PANEL_LISTENER_TOOL` and `PanelListenerOutput`, wired through `profile.listener_tool`.
- **Do not edit `LISTENER_TOOL`.** `DEBATE_LISTENER_TOOL` spreads its properties and required list, so editing it changes the debate tool.
- `Analysis.candidate_request` defaults to `"none"`; `merge_analyses` takes the last non-none.

**Which wins.**

| Detector | Listener | Result | Trace `request_source` |
|---|---|---|---|
| hit X | anything, or failed | X | `rules` (`rules+llm` if equal; `request_disagree: true` if different) |
| none | Y, turn at most 30 words, Y is not `end` | Y | `llm` |
| none | `end` | none (an end needs a rule hit) | `llm_ignored` |
| none | Y, turn over 30 words | none | `llm_ignored` |
| none | none or failed | none | `none` |

**Honesty of the claim.** The invariant "every time" is proved for the fake session and the labelled corpus, and it holds with the listener failing, because rules run on raw text. On real Prisma output, recall is unknown: `# UNVERIFIED:` our variant lists have not met real transcripts of these phrases.

**After a request.**
- `change_topic` adds that segment's unchallenged claims to `challenged_claim_ids` so R4 cannot drag the candidate back. The report still shows their verdicts.
- `avoid_terms` ("tourism" from "X ke alawa", "other than X") stay in memory, go to the actor as `<avoid_topics>`, and a sentence containing one is refused by the gate.
- Bypassing the honesty rules: "pata nahi, aage badhiye" takes the request path. It is never pressured.

**Latency.** Regex plus director is on the order of milliseconds. Repeat needs no actor. Code prefaces go to Timbre before the actor returns, as `preface` does already, so hand-offs sound sooner. The fast path remains listener + actor only.

**Known limit.** A transcript arriving after the final-wait budget (`note_late_final`) cannot change the chosen move. The next turn catches it only if the candidate says it again.

## 4. Decision 3: form, topics, fact gate

**Schema: keep `CandidateForm` unchanged.** It already holds Ruling 5's fields, has no identifier, and `extra="forbid"` rejects a name field.

**Sources.** The page's form (`parse_form`, existing), CLI prompts and script lines (existing), and new `--form-file path.json`. `examples/forms/sample_form.json` is synthetic, and it matches `DEFAULT_FAKE_FORM` plus an optional subject. No form is stored beyond what traces already hold.

**Removal of hard-coded topics.**
- `Settings.topics` is deleted.
- `cards.py` gains `discover_card_topics(cards_dir)`: `*.json` stems except `schema`, sorted. `render_cards_block` and `card_set_version` already sort, so the debate prefix should be unchanged. The P0 hash proves it.
- `deps.py`, `cli.py`, `voice/server.py` use discovery.
- `speech/keyterms.py` drops the `tourism` and `internal_security` lists and builds terms from the agenda.
- `BACKGROUND_TOPIC` goes with the UPSC cutover. Check debate's imports first.
- `guard_question` and the fake actor stop building "Ab X par aate hain".

**What debate keeps.** `ROUNDS`, motion `card_topics`, its 600 s limit, 9-turn cap, personas and prompts. Nothing else changes.

**Agenda.** Five fixed segment ids, which are the panel's structure, with content from the form:

| id | Member | Budget | Seeds from |
|---|---|---|---|
| `form_hr` | Chairperson | 2 | opener; work experience or motivation |
| `home_district` | Member 1 | 3 | `district or home_state` |
| `degree` | Member 2 | 3 | `degree_subject` |
| `interests` | Member 3 | 3 | hobbies, optional subject; generic if empty |
| `current_affairs` | Member 4 | 4 | candidate-chosen issue; one `must_ask` DM/SP situation |

`state.topics` holds these ids. Persona YAML `topics` map them to members, so **`strategist.py` needs no change**: R5 already emits `move_on` to the next "topic", and budgets use `max_turns_in`.

**Fact rules.**
- Seeds are questions only. `seed_violations()` reuses `PLACE_FACT_PATTERNS`, `CLOSED_STARTS`, `OPEN_ENDED_MARKERS`, `quiz_question`, and adds: no digits, ends in `?`, at most 28 words.
- **Current affairs: no facts source exists, and Nova's knowledge may be stale.** Seeds make the candidate choose the issue. Validator rule: in `current_affairs`, any capitalised non-initial word not in the form or a small allowlist fails. So no named scheme, event or person.
- The DM/SP situation comes from a fixed, hypothetical template bank (flood relief, a contract-labour dispute, water sharing between villages), chosen by stable hash. It avoids communal, party or caste framing.
- `allowed_facts` keeps the opener's rule: only non-synthetic cards. Today that is empty, so real runs get no facts.
- **New gap to close.** `SentenceGate` today checks only quiz questions and quoting. The opener's place-fact check does not cover other lines, and this phase makes the board name places far more. Add `check_place_facts`: refuse `PLACE_FACT_PATTERNS` and digits in panel lines (not fact challenges, which have their own guard).
- `# UNVERIFIED:` patterns are conservative. A plain invented sentence ("Kanpur Ganga ke kinare hai") passes them. The prompt forbids it, but the guard cannot prove it. State this as a limit, not a guarantee.
- Fact checks: unchanged. Rulings 29/36 hold through `build_deps`; with no real cards, no librarian call and the report says so.
- In fake runs, a test card's fact challenge is spoken by the current segment's member, since no persona owns `tourism` any more.

## 5. Decision 4: conversation memory

**Hot lane: no new model call.** The listener already gives English `stance` and `reasons` per turn. These are paraphrases, so they carry fewer ASR errors.

- `commit_last` adds `TurnNote(segment_id, member, question, gist)`. `gist` is stance plus up to two reasons, capped at 30 words. For a fallback analysis the gist is empty, never raw text.
- `summary`: the notes joined oldest to newest, capped at 120 words, dropping oldest first but keeping one note per finished segment.
- `recent`: last 3 exchanges (board line + gist).
- Only the **latest** answer goes raw, as `<candidate_speech>` today, which the quote gate checks.

**Actor user message additions** (only for panel; `panel=None` keeps the message unchanged):
`<agenda_item>` (segment title, seed, member's domain), `<conversation_summary>`, `<recent_exchanges>`, `<avoid_topics>`. Stable prefix stays stable, so Nova's prompt cache is not affected. Expected increase: 150-250 tokens (`# UNVERIFIED:` effect on first-token time).

**Optional, below the cut line.** A background `summarise_segment` call at each hand-off (`summary.v1`, background lane), result delivered through `commit_last`, replacing that segment's notes. Never awaited. On failure the notes stay.

**Prompts (new files, CHANGELOG lines; old files untouched).** `actor.v4`, `listener.v4`, `opener.v2` (no greeting, ritual has it), `agenda_plan.v1`, `judge.v2`, optional `summary.v1`. All marked not evaluated on Nova.

**Script and language.** Ruling 39 still governs. Fixed lines exist per `mix` (`hi-en`, `en`, `hi`), like `CLARIFY_LINES`.

## 6. Decision 5: personas and voices

**"Dr. Iyer" exists**: `brain/personas/economist.yaml`, and the golden file. `chairman.yaml` and `ips_officer.yaml` also exist; reuse all three.

| Persona file | Name | Domain | Segment |
|---|---|---|---|
| `chairman.yaml` (edit) | Chairperson Sharma | Form, HR, ritual | `form_hr` |
| `administrator.yaml` (new) | Smt. Menon | State, district, local administration | `home_district` |
| `economist.yaml` (edit) | Dr. Iyer | Academic: degree subject | `degree` |
| `humanities.yaml` (new) | Prof. Sen | Hobbies, optional subject | `interests` |
| `ips_officer.yaml` (edit) | Shri Rathore | Governance, DM/SP situations | `current_affairs` |

All are fictional, with the existing header line "Not modelled on any real person". Each new one needs `fallback_lines` and `filler_lines`. `economist.yaml`'s style says "Asks for numbers", which contradicts Ruling 36; change it. The orchestrator may swap the two new surnames; nothing depends on them.

**Who speaks.** The segment's member speaks all lines, including reassure and clarify (the director overrides chairman defaults). The Chairperson speaks hand-off prefaces, ritual and close.

**Voice mechanism, config only (Ruling 39).**
- New setting `tts_panel_voices: dict[str, str] = {}`, persona id → voice, from env JSON. **Empty default: every member uses `TTS_VOICE`**, exactly as today. No voice id is decided here.
- At start, each value goes through `check_voice(model, voice)`.
- Candidates for the owner's lab, all in our catalog copy (`gnani_protocol.py`): Vikrant, Omkar, Poorvi, Nalini, Deepak, Hemraj, Urmila (v2.5); Avinash, Yashraj, Vasudha (v3.0). Only six of these are in the voice lab today (`voice/lab.py`), so the owner may need the persona candidates added there to audition them.

**Three code facts the builder must respect.**
1. `TTSUpdateSettingsFrame` exists in 1.5.0 and is an `UninterruptibleFrame`, so a barge-in cannot drop it, and it goes through the same queue as `TTSSpeakFrame`.
2. **That frame alone would do nothing for us.** `TimbreTTSService.request_body` sends `self.voice`, while the plugin updates `_settings`. Worse, the plugin's validation rejects v3.0 voices. So `TimbreTTSService` must override `_update_settings`: `check_voice`, set `self.voice`, skip plugin validation for voice.
3. The plugin docstring says voice changes apply to the next request without a reconnect. `# UNVERIFIED:` on the real server.

**Voice loop.** `SpokenSentence` gains `member` (default ""). `brain_processor` sends a settings frame when the voice differs, and **sends one at each line start anyway**. Sentences held for `line` or `first_rest` must flush at a speaker change. Debate's `voice_for` returns None, so no frames are sent.

## 7. Decision 6: modes and the golden

**Rollout: build dark, then one cutover commit.**
- `UPSC_PROFILE.session_hooks = lambda s, o: PanelHooks(s, o) if isinstance(o, PanelSetup) else None`. The factory returning None keeps today's path, and about 700 tests stay untouched until P7.
- `PanelHooks` lives in `modes/upsc_panel/`, with the same hook surface as `DebateSessionHooks`, and reuses session's `_apply` where it can.
- Every shared-file change has a neutral default: `BoardMove.spoken_preface/preface_speaker/seed`, `Analysis.candidate_request`, `SpokenSentence.member`, optional hooks read through `getattr`.

**New moves.** `Move.repeat_question`, `rephrase_question`, `next_question`, added to `PRESSURE` and to `UPSC_MOVES` only. `DEBATE_MOVES` stays the same.

**Debate lock (P0, before anything else).** Add `tests/golden/debate_cli_fake.txt` and a hash test on debate's shared prefix, listener tool JSON and prompt versions. Today's debate board lines md5 is `f2a23f33b90df75103f505a2696e6944`, by the same `grep '^\['` method below.

**UPSC golden.**
- Precisely: `826ef55…` is the md5 of `cli --fake --script examples/fake_session.txt | grep '^\['`, the board lines only (full value `826ef550999cc34d9018806dce1b665e`, reproduced today). The golden **file**'s md5 is `f6b2c9a9…`, a different number. Whoever checks must use the same method.
- It stays byte-identical through P0-P6. **It changes exactly once, in P7**, in a commit with no other logic change to the golden test.
- **Review.** The builder pastes old and new board lines side by side in the report. The CTO reads every new line against the checklist in §10. The orchestrator re-runs, then records the new board-line md5 in the log, and only then accepts.
- ADR-009's "UPSC byte-identical" consequence ends at that point. ADR-010 records this deliberately.

## 8. Decision 7: utilities

**Utility A: likely questions.** `python -m mockboard.cli --likely-questions --form-file ...`; `make likely-questions` (fake, free).
- Uses the same agenda code, so template mode works with no Nova call. A `--real` run needs approval.
- Each seed has `hint`: an angle or structure, never a fact ("Start from what you have seen yourself, then one trade-off"). `hint` passes the same validator; failure uses a template hint.
- Page button if time allows (below cut line).

**Utility B: report by member.** New optional `SessionReport` fields with empty defaults: `by_member`, `weakest`, `practice_plan`.
- **`by_member` is built by code.** Turns grouped by `topic` (segment id), with the member, questions, requests made, and the judge's existing verified evidence quotes for those turns. No extra model output, so nothing new to verify.
- **`weakest` is chosen by code, not the model.** Score each turn: honest unknown, thin answer, one-sided, pressured. **Exclude** garbled, fallback-analysis, request and silent turns: a bad transcript must never read as a weak answer.
- **Outline.** `judge.v2` adds `weakest_outline`: 3-5 structure points for that turn id. `outline_violations()` refuses digits, place-fact patterns, card-less claims, and points over 20 words. On failure or judge fallback, use a fixed template (define terms, two sides, who is affected, what you would do). No outline claims a fact, unless cited from a real card. The reports' existing `model_answer` stays.
- **Practice plan: template lines by code**, keyed by lowest-scoring dimensions and the weakest segment. Up to 3 items. `insufficient_evidence` dimensions are skipped.
- **Replay this moment.** Agenda of one segment, seed = the flagged question verbatim, budget 1, no judge. That reuses the whole pipeline. Requires saving `<session>.report.json` beside the text report. Compares before and after with code: word count, reasons, balance. CLI first; page button below the cut line. Only `weakest` is offered, so only flagged moments replay (Ruling 5).

## 9. Decision 9: session length and Prisma

**The brief's sentence is wrong as worded, and the real limit is elsewhere.**
- **15 minutes is a cap per socket, not per session.** `PrismaSTTService` opens a new socket after `rotate_after_s` = 12 min when the candidate is not speaking, and unconditionally at `rotate_hard_s` = 14 min. A 20-minute session uses two sockets and is legal.
- **Correct wording:** "Session about 15-20 minutes. Each Prisma socket stays under Gnani's 15-minute cap through rotation at 12-14 minutes."
- **Three things block 15-20 minutes today.** `voice_session_max_s = 600` (10 minutes, shared with debate), `max_turns = 9` (session, CLI, server), and a hard stop that ends without a close.
- **Rotation has never run on a real session.** The 10-minute stop is below the 12-minute trigger, so only fake-server tests cover it. `# UNVERIFIED:` (in the code too) two sockets open at once, and `commit` on the old one. This phase's first long session is its first live rotation.

**Design.**
- Mode hook `session_max_s()`: panel returns `voice_panel_session_max_s = 1200`; debate keeps 600.
- `voice_panel_wrap_up_s = 1080`: voice timer calls `session.request_wrap_up()`, and the next route closes with the Chairperson's line. The candidate is never cut off mid-interview.
- Panel `max_turns`: budget sum (15) + 4 for requests. Repeat and rephrase do not use budget.
- The page's `max_minutes` follows the hook.
- A `must_ask` situation replaces a budget `move_on` or close once if unasked, unless wrap-up or a request says otherwise.
- **Length estimate (our assumption, not measured):** 15 answers × (about 50 s answer + 9 s line + 2.5 s latency) ≈ 15.5 minutes. Wrap-up handles longer talkers.
- **Cost.** A session now streams up to twice as long and speaks about 15-20 lines rather than 9-12. `# UNVERIFIED:` per-minute rate; the owner's dashboard reading is still outstanding. `VOICE_QUICKSTART.md` needs its cost section updated.

## 10. Decision 8: acceptance mapped to tests

| # | Brief's acceptance | Test | Where |
|---|---|---|---|
| A1 | The Hinglish sentence moves on with an ack, every time | `test_change_topic_always_moves_on`: at each segment and position, Latin, Devanagari, English, ASR-garbled, listener OK, failed and timed out; next line's segment differs, ack present, member changed; closes in the last segment | `tests/test_panel_invariants.py` |
| A1b | Corpus | `test_request_corpus`: about 150 labelled synthetic lines; all brief sentences hit; **zero** false `end`; zero false `change_topic` on negatives; recall reported | `tests/test_requests.py`, `tests/data/request_corpus.jsonl` |
| A1c | Never returns | `test_refused_topic_never_returns`: no later line has an avoid term; no R4 on that segment's claims | invariants |
| A2 | No hard-coded topics | `test_no_topic_list_in_source` (no `Settings.topics`; no `tourism`/`internal_security` in `config.py` or `keyterms.py`); `test_agenda_depends_on_form` (two forms, different seeds, same ids) | `tests/test_agenda.py` |
| A3 | Each line traces to item and member; hand-offs spoken | `test_every_board_line_traces`, using `eval/panel/check_trace.py`; `test_handoff_is_spoken_at_every_segment_change` | invariants |
| A4 | Guards hold | Existing `test_ruling36_37_lines` kept; bad-actor test (fake line quotes, quizzes, states a place fact: never heard); `test_fallback_agenda_passes_seed_validator` over 20 synthetic forms | invariants |
| A5 | Latency | `test_turn_llm_calls`: per turn only `listener` and `actor`, across a session; `plan_agenda` exactly once; `test_director_budget`: under 10 ms at p99 offline; LLM-raises test | `tests/test_latency_budget.py` |
| A6 | Everything keeps passing; debate unaffected | Full suite; debate golden and prefix hash; chaos adds faults on `plan_agenda`, sessions still finish with template agenda | `test_modes.py`, `test_chaos.py` |
| A7 | Real session | **Owner-approved only.** `python -m eval.panel.check_trace traces/<id>.jsonl` plus the orchestrator's reading | P12 |

**More invariants.** Reopened turn leaves memory and position unchanged; `repeat` says the last question exactly and uses no budget; budgets never exceeded; `must_ask` asked unless skipped; every session ends with the fixed close; plan failure, timeout and late arrival; agenda's `state.topics` equal to `SessionState` position each turn; existing honesty and pressure invariants, with request turns exempt from the "only reassure or move_on" check but still never pressured.

**Trace fields.** Turn line, new block `panel`: `agenda_source`, `segment_id`, `segment_index`, `seed_id`, `member`, `member_name`, `route`, `handoff_from`, `preface_speaker`, `candidate_request`, `request_source`, `request_disagree`, `request_pattern`, `avoid_terms`, `turns_in_segment`, `memory_words`, `director_ms`, `director_error`. Events: `session_start` adds `agenda` (ids, members, source) and the opener's `segment_id`, `seed_id`, `member`; plus `agenda_upgraded`, `wrap_up`. `summarize.py` gains a panel table with no spoken text, so it stays safe to paste.

**CTO's checklist for reading the fake session:** ritual; five members in order; a spoken hand-off at each change; each question follows the last answer's gist; request handled; no "Exampleland" outside `--fake`; closing line; report by member.

## 11. Disagreements for the orchestrator to rule on

- **D1. Fast and fact-check graphs are not subgraphs, and `understand_turn` does not wrap the listener.** The brief says "subgraphs or called from these nodes". Either would put waiting inside the turn and break Ruling 9. They feed the director's input instead.
- **D2. `close` does not run the report graph.** `session.finish()` keeps it, since the voice loop must speak first and the report takes seconds. "Then" holds; the graph edge does not exist.
- **D3. Separate `plan_agenda` graph.** The brief lists it as a node of the director. Separate means the per-turn graph is provably model-free.
- **D4. Brief's session-length wording** should be corrected (§9). Raising the limit to 20 minutes costs more per session: the owner should agree, since credits are finite.
- **D5. Ruling 5 says replay "reuses cached board audio".** No board audio cache exists. I propose one fresh Timbre line per replay, amending the ruling, or dropping audio for a text-only replay.
- **D6. The Chairperson speaks all hand-offs,** including from another member's segment. It is the brief's example and costs one voice switch rule.
- **D7. An `end` request needs a rule hit,** so a listener mistake cannot end a paid session. No confirmation step (time).
- **D8. Fixed segment ids** are structure, not topics: "agenda comes from the form" is met by content. Confirm that reading of A2.
- **D9. CLAUDE.md ("Real-service checks: you can run them directly") and `.claude/agents/builder.md` ("real-service calls only when the brief asks") now conflict with the owner's rule.** I do not edit them; the orchestrator should.
- **D10. CLAUDE.md's "722 tests"** is out of date: I saw 821 with the voice extra.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Detector misses real Prisma text | Listener field as second net; request-corpus additions from any approved transcripts; trace shows `request_source` |
| False `change_topic` skips a segment | Position rule, negatives in corpus, `request_disagree` in trace |
| Actor still ignores memory | `<agenda_item>` always supplies the seed; verbatim seed as fallback; judged only in a real run |
| Voice switch adds latency or fails | Default is one voice; turn off by leaving config empty |
| First live rotation fails during the demo | Test once in the approved long session; a demo recording can stay under 12 minutes if it fails |
| Scope exceeds a week | Dark build, cut line below; P7 is the only risky commit |
| Director and session disagree on position | Session owns position; invariant asserts it |

## 13. Paid checks needing the owner's approval (none run)

1. **Timbre per-request voice switch** on one socket, and its latency (a few short lines).
2. **Nova `plan_agenda`** time and seed quality (1-3 calls).
3. **Nova `listener.v4`** on request phrases: labels and latency (about 20 lines).
4. **Nova `actor.v4`** first-token time with memory, and whether it obeys the no-facts rule.
5. **Nova `judge.v2`** outline through the guard (1-2 calls).
6. **`make stt-ab`, `make voice-lab`**: already owed (Ruling 39). They also give real transcripts for the corpus.
7. **`make voice-upsc`, one session of over 12 minutes:** A7, plus first live rotation, two sockets, and credits used. This is the most expensive.

Items 2-5 can share one `make cli-real` session, which is cheap compared with voice.

## 14. Build order and cut line

| Day | Work |
|---|---|
| Nov 1-2 | P0-P3 |
| Nov 3 | P4-P5 |
| Nov 4 | P6-P7 |
| Nov 5 | P8-P10 |
| Nov 6 | P11; P12 with approval; fixes |
| Nov 7 | EC2 deploy (V8) |
| Nov 8 | Demo video, write-up |
| Nov 9 | Submit, with spare time |

**Cut in this order if late:**
1. `summary.v1` (never built by default).
2. Page buttons for Utility A and replay.
3. Replay itself (keep the weakest question and outline).
4. Distinct voices at runtime (P8); the panel still works in one voice with spoken names.
5. Nova `plan_agenda` (P6); template agenda only.

**Never cut:** detector, fallback agenda, director, hand-offs, memory notes, close, trace checker, by-member report. **Hard rule: if P7 is not accepted by end of Nov 5, stop adding anything else until it is.** EC2 and the video do not move.

## 15. Chunk plan

Every chunk: `make test` and `make lint` green, small commits, §0 applies, no real calls. "Verify" is a fake session the reviewer can run. Size: S under 2 h, M 2-4 h, L 4-6 h.

**P0. Safety nets (S).**
- *Goal:* lock debate; add the form file.
- *Files:* `tests/golden/debate_cli_fake.txt`, `tests/test_modes.py`, `examples/forms/sample_form.json`, `cli.py` (`--form-file`), `docs/adr/ADR-010-interview-director.md` (draft).
- *Tests:* debate golden; prefix, tool and prompt-version hashes; form file rejects an unknown field.
- *Verify:* `make cli-fake` board-line md5 still `826ef55…`; debate `f2a23f33…`.

**P1. Request detector (M).**
- *Goal:* `detect_request` and corpus; no wiring.
- *Files:* `brain/requests.py`, `tests/test_requests.py`, `tests/data/request_corpus.jsonl`.
- *Tests:* A1b.
- *Verify:* the builder's report prints the per-kind recall and false-positive table; label it synthetic.

**P2. Agenda, personas, fixed lines (M).**
- *Goal:* types, fallback agenda, validator, personas.
- *Files:* `brain/schemas.py`, `brain/agenda.py`, `brain/panel_lines.py`, `brain/personas/{administrator,humanities}.yaml`, edits to the three existing YAMLs **only if** the UPSC golden stays identical (else defer those edits to P7).
- *Watch:* new persona files enter the UPSC prefix. `render_persona_pack` output changes printed `prefix ~` tokens in the golden header, so the new YAMLs go in a `panel/` subfolder until P7, loaded by `load_personas(PERSONA_DIR / "panel")`; `glob` is non-recursive.
- *Tests:* `test_agenda.py` (fallback passes validator; injection-like form text stays inert; empty hobbies).
- *Verify:* a test prints the sample form's agenda; golden unchanged.

**P3. Director policy and graph (L).**
- *Goal:* `decide_panel` pure function; `brain/director.py` graph with checkpointer; moves added.
- *Files:* `brain/director.py`, `brain/schemas.py`, `strategist.py` (`PRESSURE` entries only), `modes/upsc.py` (`UPSC_MOVES`).
- *Tests:* each route; repeat invocation idempotent; commit folds once; LLM-raises; thread deleted; fallback to pure function.
- *Verify:* unit-level trace of a scripted decision sequence; both goldens unchanged.

**P4. `PanelHooks`, dark (L).**
- *Goal:* a complete panel session in fake, with `PanelSetup`.
- *Files:* `modes/upsc_panel/{hooks,setup}.py`, `session.py` (optional `decide_turn`, `opening_ritual`, `request_wrap_up`; `spoken_preface`), `brain/actor.py` (preface rules, seed fallback, `panel` argument, gate `check_place_facts`, avoid terms), `prompts/actor.v4.md`, `opener.v2.md`, `brain/fake_responders.py`, `cli.py` (hidden `--panel`), `examples/fake_panel.txt`, `tests/test_panel_invariants.py`.
- *Note:* v4 prompts are versioned per panel, so `prompt_versions` for old UPSC stays until P7.
- *Tests:* A1, A1c, A3, A4; reopen; budgets; `must_ask`.
- *Verify:* `cli --fake --panel --script examples/fake_panel.txt`, read by a human; both goldens unchanged.

**P5. Listener field and memory quality (M).**
- *Files:* `brain/tools.py` (`PANEL_LISTENER_TOOL`), `brain/listener.py`, `prompts/listener.v4.md`, `brain/schemas.py`, `strategist.merge_analyses`, `fake_responders.py`.
- *Tests:* resolution table row by row; debate tool hash unchanged; fake listener disagreeing with rules; memory cap; no raw text in `summary`.
- *Verify:* a script where only the listener catches a request (`request_source: llm`).

**P6. `plan_agenda` graph (M).**
- *Files:* `brain/graphs.py` or `brain/agenda.py`, `tools.py` (`AGENDA_TOOL`), `prompts/agenda_plan.v1.md`, hooks, `voice/brain_processor.py` (one `opening_ritual` call), `fake_responders.py`, `tests/test_chaos.py`.
- *Tests:* slow, failed, invalid and late plans; one bad seed replaced alone; A6 chaos.
- *Verify:* fake session with an injected failure still completes; `agenda_source` shows it.

**P7. Cutover (M, the risky one).**
- *Goal:* panel becomes the UPSC path.
- *Files:* `modes/upsc.py`, `config.py` (delete `topics`; prompt versions; new session settings), `cards.py`, `deps.py`, `cli.py`, `voice/server.py`, `voice/pipeline.py`, `speech/keyterms.py`, personas moved up from `panel/`, `examples/fake_session.txt`, `tests/golden/upsc_cli_fake.txt`, affected tests, `eval/runner.py` as needed.
- *Migrate tests* by passing today's expectations through the panel, not by weakening them. Any invariant loosened must be named in the report.
- *Tests:* A2, A5.
- *Verify:* §7 golden review; `make cli-fake`, `cli-fake-debate`, `eval-gate`, `eval-debate`, `voice-fake`.

**P8. Voice per member, and timers (M).**
- *Files:* `speech/timbre_tts.py`, `voice/brain_processor.py`, `voice/lines.py` if needed, `session.py` (`SpokenSentence.member`), `config.py`, `voice/pipeline.py`, `tests/voice/*`.
- *Tests:* frame order with a fake TTS; barge-in between speakers; flush for `line` and `first_rest`; v3.0 voice accepted; empty config sends no change; wrap-up then close; long-run fake with short rotation.
- *Verify:* `make voice-fake`, with two members configured: fake TTS records the voice per request.

**P9. Utility A (S).**
- *Files:* `cli.py`, `brain/agenda.py`, `Makefile`, `tools.py`, prompt as needed, tests.
- *Tests:* each hint validated; template mode makes zero LLM calls.
- *Verify:* `make likely-questions`.

**P10. Utility B (L).**
- *Files:* `brain/schemas.py`, `brain/report_panel.py`, `brain/judge.py`, `tools.py` (panel judge tool; check debate's is separate), `prompts/judge.v2.md`, `graphs.py`, `cli.py` (`format_report`, `--replay`), `voice/brain_processor.py` (save JSON), `fake_responders.py`.
- *Tests:* garbled and fallback turns never weakest; outline guard; judge fallback still has by-member and plan; report validators; replay of one question.
- *Verify:* read the fake report; `cli --fake --replay <report.json>`.

**P11. Trace checker and docs (S).**
- *Files:* `eval/panel/check_trace.py`, `voice/summarize.py`, `Makefile`, `CLAUDE.md` via orchestrator only, `docs/WALKTHROUGH.md`, `docs/VOICE_QUICKSTART.md`, ADR-010 final, CHANGELOG.
- *Tests:* checker passes a good trace and fails a tampered one.
- *Verify:* checker on a fake trace.

**P12. Real session (owner approval required, no builder).**
- *Goal:* §13 items, in the order the owner approves.
- *Verify:* orchestrator reads the trace with `check_trace`, and prompts or thresholds get one fix chunk.

## Sources
- LangChain docs: https://docs.langchain.com/oss/python/langgraph/persistence and https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- Installed sources: `langgraph` 1.2.17, `langgraph-checkpoint` 4.3.0, `pipecat/services/tts_service.py`, `pipecat/frames/frames.py`, `pipecat_gnani/tts.py` 0.5.12.
- Gnani voice names: our catalog copy in `src/mockboard/speech/gnani_protocol.py`, not re-fetched today.
