# Mock Board walkthrough

This guide explains how Mock Board works, in the order you would explain it to someone else. Each part starts with the idea and the design thinking, then gives a simple analogy with an example, then shows a few lines of the real code, and ends with how that part is tested or evaluated. You do not need to memorise the code. The goal is that you can explain every box in the system, in your own words, in an interview.

Everything here describes the code as it is on October 26, 2026 (text mode only). Where a number is a placeholder that has not been measured yet, it is marked UNVERIFIED, as it is in the code.

## 1. The big picture: fast mouth, slow brain

A voice interview has one hard rule: when the candidate stops talking, the board must start talking within about two seconds, or the silence feels broken. But a good board also has to think: what did the candidate claim, is any figure wrong, was the answer one-sided, is the candidate nervous? Thinking takes several model calls, and each call to Amazon Nova Pro in Sydney takes real time.

Mock Board solves this by splitting the work in two. The **slow brain** does almost all the thinking while the candidate is still speaking, one transcript segment at a time, and keeps the board's next move ready. The **fast mouth** is a single streamed model call, made only after the candidate stops, whose only job is to phrase that ready-made move as one short spoken line.

There is one honest exception to "the thinking happens while the candidate talks". Prisma, Gnani's speech-to-text service, sends the final transcript of a segment only after the candidate stops and its voice detector closes that segment. So the listener call for the **last** segment cannot start until the candidate has finished. Between "candidate stopped" and "board speaks" there can therefore be two model calls: the last segment's listener, which is bounded by `move_deadline_ms` (if it is late, the move computed after the previous segment is used instead), and the actor. The fact checks never sit on this path.

That is why the latency budget is "last listener plus actor", not "actor alone". Our own p95 target is 3.0 seconds from the candidate stopping to the board's first audio, split like this (these are budgets we chose, not measurements; CTO_NOTES_03 §4):

| Stage | Budget |
|---|---|
| Turn detection (Silero 0.2 s plus Smart Turn) | 0.3 s |
| Last final transcript plus the last segment's listener call | 1.0 s |
| Actor's first sentence | 1.2 s |
| Timbre's first audio byte | 0.3 s |
| Transport | 0.2 s |

The probe `python -m probes.nova_probe --app-prompts` measures the two model rows with the app's real prompts, and the deadline values in `config.py` stay UNVERIFIED until it has run from the EC2 server.

**Analogy.** Think of a school quiz-show host with a helper behind the curtain. While a child is answering, the helper listens, checks the answer against the answer cards, and writes the host's next line on a card. When the child stops, the host just reads the card aloud in their own voice. The host never has to stop and think, so there is no awkward silence. In Mock Board, the helper is the slow brain (the listener, the librarian and the strategist), and the host is the actor.

**Example.** A candidate says, "Exampleland mein tourism GDP ka 30 percent hai." While they keep talking, the listener writes down that claim, and in the background the librarian checks it against the fact cards and finds the card says 12 percent. The board does not interrupt. On the next turn, the strategist sees the checked contradiction and chooses a fact challenge, and the actor says: "Aapne 30 percent kaha. Exampleland test data ke 2023 ke figure ke hisaab se yeh 12 percent hai. Aapka figure kahan se hai?" (You can see exactly this in `make cli-fake`.)

Here is one candidate turn from start to finish:

```
candidate speaks ──► segment 0 ──► fast analysis graph (listener → strategist) ──► move ready
                 ──► segment 1 ──► fast analysis graph (listener → strategist) ──► move ready (newer)
                                   └─► fact-check graph in the background (librarian ×N → gate)
                                       └─► verdicts go into the claim ledger, used from the NEXT turn
candidate stops ──► last segment arrives ──► fast analysis graph (listener → strategist)
                ──► end_of_turn: take the latest ready move (wait at most the deadline)
                ──► speak: the actor phrases the move
                    (critical path = last segment's listener + actor)
```

## 2. The session API: submit_segment, end_of_turn, speak

`src/mockboard/session.py` holds `InterviewSession`, which runs one interview. The future voice loop and today's text CLI talk to it through exactly three calls, so the brain does not care whether the words came from a microphone or a keyboard.

1. **`submit_segment(text, segment_index=k)`** is called for each final transcript segment, as soon as it arrives. (Gnani Prisma sends one final transcript per segment that its voice detector closes; the CLI sends one per typed answer, or more if you type ` | `.) It never blocks. The first segment of a turn "opens" the turn: the session freezes a snapshot of the facts it knows so far (the claim ledger) and the turn's context (topic, previous move, counters). Then it queues that segment's fast analysis.
2. **`end_of_turn(deadline_s)`** is called when the candidate stops speaking. It waits for this turn's queued fast analyses, but never longer than the deadline, and returns the board's move. If everything finished, it uses the move computed after the last segment. If the deadline passed but some segments finished, it uses the move from the latest finished one. If nothing finished, it uses a deterministic fallback rule: silence gets `reassure`, anything else gets a neutral `probe`. Every such fallback is written into the trace as `fallback_reason`.
3. **`speak()`** asks the actor to phrase the move, updates the session's counters with plain code, and writes one trace line for the turn.

This is how the CLI uses them (`src/mockboard/cli.py`):

```python
for index, part in enumerate(typed_segments(text)):
    session.submit_segment(part, segment_index=index)
await session.end_of_turn(args.deadline_s or settings.text_move_deadline_s)
out = await session.speak()
```

**Design thinking.** Two choices here matter a lot. First, fact checks are never awaited by a move. They run in the background, and their verdicts reach the strategist only on a later turn. Second, the ledger snapshot is frozen when the turn opens, so every segment of a turn sees the same facts, and a verdict that arrives in the middle of a turn waits for the next one. This means the board's move never depends on how fast a fact check happened to be, which makes behaviour reproducible. A config validator refuses `fact_challenge_min_delay_turns < 1`, so nobody can accidentally make the board wait for a same-turn verdict.

**Analogy.** Think of a post box. Each transcript segment is a letter dropped in the box (`submit_segment`). The sorting office starts working on each letter straight away. `end_of_turn` is the last collection time: whatever reply has been sorted by then goes out, and if nothing is ready, a standard reply goes out instead of nothing. `speak` is the postman reading the reply aloud at the door.

**How it is tested.** `tests/test_latency_budget.py` uses fake models with injected delays to prove that `end_of_turn` returns within its deadline even while fact checks are slow, that a verdict found during turn N changes the move on turn N+1 and never on turn N, and that segments are analysed in order. `tests/test_cli.py` runs the scripted CLI end to end.

## 3. LangGraph through our two graphs

LangGraph is a library for writing a workflow as a graph: boxes (nodes) that do work, arrows (edges) that say what runs next, and a shared state that flows through them. Mock Board uses LangGraph only for the slow brain, never for the actor (see ADR-002). There are three graphs in `src/mockboard/brain/graphs.py`: the **fast analysis graph**, the **fact-check graph**, and a small **report graph**. Each LangGraph idea is introduced below the first time one of our graphs needs it. The snippets are copied from the real code and shortened; `...` marks where something was left out.

### 3.1 State

The state is a typed dictionary that every node can read, and that each node updates by returning only the keys it wants to change. Our fast graph's state is `FastState`:

```python
class FastState(TypedDict, total=False):
    session_id: str
    turn_id: str
    segment: Segment
    ctx: StrategyContext
    ledger: list[Claim]  # input snapshot, overwritten by every run
    analyses: Annotated[list[Analysis], operator.add]
    moves: Annotated[list[BoardMove], operator.add]
    fallbacks: Annotated[list[dict[str, str]], operator.add]
    move: BoardMove
```

(Some fields are left out here for space.) **Analogy:** the state is a class notebook that is passed from child to child. Each child writes only in their own section.

### 3.2 Nodes

A node is an ordinary async function. It receives the current state and returns a dictionary of updates. Our strategist node is a good small example (the real one also wraps `decide_move` in a `try`, so a bug falls back to a neutral probe instead of stalling the board):

```python
async def strategist(state: FastState, runtime: Runtime[BrainDeps]) -> dict[str, Any]:
    deps = runtime.context
    ctx = state["ctx"]
    analyses = [a for a in state.get("analyses") or [] if a.turn_id == ctx.turn_id]
    move = decide_move(ctx, analyses, state.get("ledger") or [], deps.strategy)
    return {"move": move, "moves": [move], ...}
```

`runtime.context` is how a node gets its tools (the model client, the cards, the settings) without global variables. We pass one `BrainDeps` object as the run's context (`graph.ainvoke(..., context=deps)`), and tests pass a `BrainDeps` built with a fake model.

### 3.3 Edges

Edges say which node runs after which. The fast graph is a straight line:

```python
g = StateGraph(FastState, context_schema=BrainDeps)
g.add_node("listener", _make_listener(...), retry_policy=retry_policy(...))
g.add_node("strategist", strategist)
g.add_edge(START, "listener")
g.add_edge("listener", "strategist")
g.add_edge("strategist", END)
```

So for every segment: the listener reads it, then the strategist decides a move. This is the only work allowed between end of turn and the actor call.

### 3.4 Reducers

When a node returns `{"analyses": [analysis]}`, should that replace the old list or add to it? A **reducer** decides. `Annotated[list[Analysis], operator.add]` means "add the new items to the end", so the list of analyses grows with every segment. A field without a reducer, such as `ledger` or `move`, is simply overwritten. We want exactly that for the ledger: every run receives a fresh snapshot. **Analogy:** a reducer is the rule for how new writing joins the notebook. For some pages you add a new line under the old ones; for others you rub out the old answer and write the new one.

### 3.5 Checkpointer

A checkpointer saves the state after every step of the graph (LangGraph calls each step a "super-step" and creates a checkpoint at each super-step boundary), keyed by a `thread_id`, so the next run on the same thread starts from the saved state. We compile the fast graph with an in-memory checkpointer and use the session id as the thread id:

```python
cfg: dict[str, Any] = {"configurable": {"thread_id": session_id}}
```

That is why the strategist, when it runs for segment 2, can see the analyses of segments 0 and 1 of the same turn: they were saved on the session's thread. Because two runs on one thread at the same time would race on the saved state, `TurnAnalyzer.submit` chains each new run behind the session's previous one, so segment k+1 can never overtake segment k. **Analogy:** the checkpointer is a bookmark for each session's notebook, so the next child opens it at the right page.

### 3.6 Retry policy

A node can be given a `RetryPolicy`: if it raises a matching error, LangGraph runs it again after a short wait. Ours retries only our own "transient" error type (throttling, timeouts, server errors), never a configuration mistake:

```python
return RetryPolicy(
    max_attempts=max_attempts,
    initial_interval=initial_interval_s,
    backoff_factor=2.0,
    jitter=jitter,
    retry_on=is_transient,
)
```

Retries happen in exactly one place per call: botocore's own retries are switched off on every Bedrock client, so attempts never multiply across layers. A node re-raises a transient error only while attempts remain; on its last attempt it returns a safe default instead. The builder found by reading LangGraph 1.2.17's source that its jitter adds up to one whole second per retry, so the fast graph turns jitter off and the background graphs keep it. **Analogy:** if a phone line is busy, wait half a second and try again, a fixed number of times. If the number does not exist, do not keep calling.

### 3.7 The fact-check graph: conditional edges and `Send` fan-out

The fact-check graph checks one segment's claims. It is started in the background by the session whenever the listener found claims, and no move ever waits for it.

```
START --(Send per claim)--> librarian (×N, in parallel) --> gate --> END
      \--(no claims)--------------------------------------------> END
```

A **conditional edge** runs a small function on the state to decide where to go next. A **`Send`** is a special return value that says "run this node with this particular input"; returning a list of them runs one copy of the node per item, in parallel. Our routing function does both:

```python
def route_claims(state: FactCheckState) -> list[Send] | str:
    claims = state.get("claims") or []
    if not claims:
        return END
    return [Send("librarian", LibrarianTask(claim=claim, ...)) for claim in claims]

g.add_conditional_edges(START, route_claims, ["librarian", END])
```

Each parallel librarian branch returns `{"votes": {key: vote}}`, and the `votes` field has a custom reducer, `merge_dict`, that merges the small dictionaries from all branches into one. Then the `gate` node turns every vote into a final verdict. **Analogy:** a teacher hands one worksheet to each of several children at the same time, and they all drop their finished sheets into one tray (the reducer). Then one checker marks the whole tray (the gate).

One important detail: LangGraph runs parallel branches as one "superstep", and if any branch raises an error, the whole superstep fails and, in a graph without a checkpointer like this one, none of the branches' results are kept. (With a checkpointer, LangGraph saves the writes of the branches that succeeded and does not re-run them when the graph resumes.) The builder tested this against LangGraph 1.2.17 before relying on it. So every librarian branch catches its own errors and returns an "unverifiable" fallback vote instead of raising, and one bad claim can never wipe out the verdicts of the others.

The fact-check graph has no checkpointer. Each run is independent and hands its checked claims back to the session, which adds them to its ledger.

### 3.8 The report graph: a loop with a limit

After the interview, a third graph writes the report:

```
START → judge → citation_check ─(a quote is missing and no revision yet)→ judge
                               └─(otherwise)──────────────────────────→ report → END
```

The conditional edge after `citation_check` can send the work back to the judge, but at most once (`MAX_JUDGE_REVISIONS = 1`). This is the only loop in the whole system, and it runs after the interview, never during it.

**How the graphs are tested.** `tests/test_graphs.py` checks the wiring: the librarian is skipped when there are no claims, one librarian call is made per claim, a failing branch leaves the other claim's verdict intact, the retry policy retries and then falls back, and the vote cache holds only real votes. `tests/test_judge.py` checks the report graph's bounded revision.

## 4. The components

### 4.1 Listener (`brain/listener.py`)

The listener turns one transcript segment into a typed `Analysis`: the candidate's stance, their reasons, whether the answer is balanced or one-sided, whether they honestly said "I don't know" (`honest_unknown`), whether they admitted a mistake (`concedes_error`), whether the transcript looks garbled, and a list of claims. Each claim is normalised into English (`claim_en`) with a `value`, `unit` and `year`, so that code can compare it with a fact card later. It is one forced-tool Nova call (see section 4.7 and ADR-004). If the model leaves a number out, plain code tries to read it from the words ("dedh lakh", "30 percent") with `parse_number_phrase`; a "numeric" claim with no number at all is downgraded to a qualitative one, because it cannot be checked as a number.

The candidate's words go into the prompt inside clear tags, and any tag-like text inside them has its angle brackets swapped for look-alike quotation marks, so nothing the candidate says can close our tag or forge a new one. That way a candidate who says "ignore your instructions" is treated as data, not as an instruction.

**Analogy.** The listener is a careful note-taker at the back of the room who writes each claim on its own index card in a fixed format: "what was said, which number, which unit, which year". Example: from "Exampleland ne 37 districts ko affected list kiya tha in 2024" the card reads value 37, unit districts, year 2024.

**How it is tested.** `tests/test_schemas.py` and `tests/test_numbers.py` check the parsing and number handling; the fake model returns malformed payloads on purpose in `tests/test_llm.py`, so the "retry once, then safe default" path is exercised. On real Nova, the eval's claim F1 (section 8) measures how well claims are extracted.

### 4.2 Librarian, numeric gate and vote cache (`brain/librarian.py`, `brain/numbers.py`)

This part decides whether a claim is supported, contradicted or unverifiable. It is split into a model step and a code step on purpose, so the model proposes and code confirms.

**The vote.** `ask_librarian` makes one forced-tool Nova call per claim. The fact cards sit in the cached system prompt rather than behind a search engine, because 40 to 60 cards per topic fit comfortably (PLAN_v2_DELTA decision 6), and the model answers with a verdict and the id of the card it used.

**The numeric check (the second vote).** `numeric_check` in `numbers.py` compares the claim's value, unit and year with the card's, using plain arithmetic. It understands lakh, crore and Hindi number words, allows 5 percent relative tolerance for ordinary quantities and 1 point for percentages, and it refuses to compare unfair things: percent versus percentage points, different units, or a figure for a different year all come out as "incomparable", never "mismatch".

**The gate.** `gate_verdict` combines the two with a small truth table. A claim becomes `contradicted` only when the model says contradicted **and** the numeric check says mismatch. A model-only contradiction on a non-numeric claim becomes `possible_conflict`, which is shown in the report but never challenged aloud. No card, or a fallback vote, always means unverifiable. The strategist then adds a second, independent check: it only challenges claims that are contradicted, numeric or dated, and cite a card. Because there are two gates, one bug cannot produce a wrong challenge. This "ask, never assert" rule is ADR-003.

**The vote cache.** The same claim often comes up again (in a later session, or in every eval run). `VoteCache` remembers votes, keyed by a hash of the normalised claim plus the card-set version, so editing any card makes all old entries unreachable. It has one rule that makes it safe to share across sessions: `put` refuses fallback votes. An earlier design used LangGraph's node cache, which stored fallbacks too, so one throttled session would have switched off fact-checking of that claim for every later user (ruling 10, ADR-006).

**Analogy.** The librarian is a school librarian checking a claim against the index cards on her desk. The numeric gate is a second person with a calculator, and the claim only counts as wrong when both agree. The vote cache is a notebook of answers already looked up, and the rule is that you only write down real answers, never "the library was closed today".

**Example.** The candidate says 30 percent; the card says 12 percent for the same year. The model says contradicted, the calculator says mismatch (18 points apart, tolerance 1), so the verdict is `contradicted`. If the candidate had said 30 percent for 2019 and the card were for 2023, the calculator would say "incomparable" and the verdict would be `unverifiable`: the board stays silent rather than risk a wrong correction.

**How it is tested.** `tests/test_librarian.py` walks through every row of the gate's truth table; `tests/test_numbers.py` checks lakh and crore, Hindi words and the percent-versus-points rule; `tests/test_graphs.py` has a two-session test proving a throttled fallback in one session is never served to another. On real data, the eval's planted-error recall and false-correction count (section 8) are the headline numbers.

### 4.3 Strategist (`brain/strategist.py`)

The strategist chooses the board's next move from nine: probe, counter, steelman, fact_challenge, pressure, reassure, move_on, clarify and interrupt. It is pure deterministic rules with no model call, so the same situation always gets the same move, and every move can be explained by naming a rule. It runs after every segment, so a move is always ready.

The rules are checked in order and the first one that matches wins. In plain words: a garbled transcript gets `clarify` (R1); an over-long answer gets `interrupt`, only if that feature flag is on (R2); silence, a long pause, or the first honest "I don't know" in a topic gets `reassure` (R3); a second honest "I don't know", or one on the topic's last question, gets `move_on` (R3b); a checked, numeric, card-backed contradiction from an earlier turn gets `fact_challenge` (R4); a used-up topic or three strong answers in a row gets `move_on` (R5); a one-sided answer gets `counter`, then `steelman` (R6); a thin answer gets polite `pressure`, within limits (R7); and everything else gets `probe` (R8).

Several rules exist because watching real sessions exposed bad behaviour. An honest "I don't know" is never punished: it can only get reassure or move on. Pressure is never used right after reassure, never twice in a row, at most twice per topic, never just after the candidate admits a mistake, and never when the listener failed (because then "thin answer" is only a guess). When the candidate concedes an error, whatever move is chosen carries `preface="acknowledge"`, and the actor first says "Theek hai, yeh maanna achhi baat hai."

**Analogy.** The strategist is a referee with a rulebook. The referee does not have opinions; they look down the list, find the first rule that fits, and blow the whistle. If you ask "why that call?", they point at the rule number. Every move in the trace carries its `rule_id` and a reason, such as "R4: claim t1.s0.c0 contradicts card … (model + numeric check agree)".

**How it is tested.** `tests/test_strategist.py` checks every row of the rule table. `tests/test_board_invariants.py` runs whole scripted and randomly generated interviews and checks properties of the conversation as a whole, for example "no pressure right after reassure" and "a conceded error is acknowledged". Each bad behaviour that was found by watching a session became one of these invariants.

### 4.4 Actor (`brain/actor.py`)

The actor turns the chosen move into one spoken line of at most 30 words, in the voice of the board member who owns the move. It is a plain streamed Nova call, not a graph node, because it is the last call on the critical path (after the last segment's listener, see §1) and must start speaking as early as possible. Its prompt (`prompts/actor.v2.md`) gets the move, the persona's style, the last question and the candidate's answer.

Three guards live in code, not only in the prompt:

1. **Word cap.** Streaming stops at 30 words, whatever the model does.
2. **Ask, never assert.** A fact challenge must end with a question, must not contain phrases like "aap galat hain", must say the card's figure, and must not read the card's text aloud. If it breaks any of these, a deterministic template replaces it. The model is given pre-rendered figures in the card's own scale ("15 lakh", "12 percent") so it never has to convert numbers.
3. **The acknowledgement** after a conceded error is a fixed sentence added by code, so it is always said and can later be played from cached audio.

**Ruling 14: one attempt, then a fallback line.** The actor gets exactly one attempt, with no retry anywhere (the only exception is ruling 17's optional fast retry, off by default). It also has a time budget: if no first token arrives within `actor_first_token_budget_s` (2.5 seconds, UNVERIFIED), the call is abandoned. When the call fails or misses the budget, the board says a line that needs no model at all. For most moves, that is one of the speaking member's own `fallback_lines` from their persona YAML file, such as "Ek minute. Aap apni baat jaari rakhiye, zameeni sthiti par aaiye." for the IPS officer. A fact challenge falls back to its deterministic template instead (the claim is marked as challenged, so the challenge must still be heard), and a topic change falls back to its move template (the topic really changes, so the board must say so). The reasoning: around eight seconds of silence while a retry runs is worse than a board member pausing naturally.

**Ruling 17: no double pause, and an optional fast retry.** In the voice loop, a short masking filler such as "Hmm..." will play if the actor's first sentence is late. If the actor then fails, the persona's fallback line would be a second pause in a row ("Hmm..." and then "Ek minute... aap apni baat jaari rakhiye"), which sounds broken and asks nothing. So once a filler has played, a failed actor call falls back to the move's own template question instead (for a probe: "Achha. Is par thoda aur detail mein bataiye, aisa kyun?"). Pipecat reports the filler with `session.mark_filler_played()`, or with `speak(filler_played=True)` if it played before `speak` was called; the actor reads the flag only when the call fails, because the filler starts while the call is still pending. Text mode never plays a filler, so nothing changes there. Separately, the setting `actor_fast_throttle_retry` (off by default) allows exactly one retry when Bedrock throttles the call within `actor_fast_throttle_window_ms` (300 ms) and before any word has arrived, using whatever is left of the first-token budget. A throttle that comes back that fast leaves time for a second try; the Nova probe's throttle timings decide whether to turn it on. **Analogy:** if the teacher has already said "Hmm, let me see...", she does not then say "One moment..." as well; she just asks a simple question from her notes.

**Analogy.** The actor is a stage actor reading the line the director wrote, in character, with a strict time limit. A script editor stands in the wings and swaps any line that accuses the candidate for a polite, pre-approved one. And if the actor forgets their lines, they do what a real teacher does while finding her page: "Go on, I am listening."

**How it is tested.** `tests/test_actor_opener.py` checks the word cap, the challenge shape (the candidate's figure first, the card's figure and year, sentence case, a question at the end) and that bad lines are replaced. `tests/test_actor_fallback.py` checks ruling 14: a failed call is not retried and the persona's line is spoken; a call past its budget is abandoned on time (on the real Bedrock client with a slowed fake); a fact challenge and a topic change keep their templates; and a whole session records the fallback in its trace and carries on. `tests/test_ruling17.py` checks ruling 17: after a filler the move's template is spoken instead of the persona line, even when the filler starts during the call; text mode is unchanged; and the fast retry happens only when it is switched on, only once, only for a fast throttle before the first token.

### 4.5 Judge (`brain/judge.py`)

After the interview, the judge writes the report. It scores the seven personal qualities that UPSC publishes for the Personality Test: mental alertness, critical powers of assimilation, clear and logical exposition, balance of judgement, variety and depth of interest, ability for social cohesion and leadership, and intellectual and moral integrity. It adds two practice indicators that are clearly labelled as Mock Board's own, not UPSC criteria: factual accuracy and composure. Each score is 1 to 5, or "insufficient_evidence" when the interview did not show that quality. There is deliberately no accent, dialect, grammar or fluency score (ruling 6). The report can be written in English or Hindi.

Every score must carry evidence: an exact quote and the turn it came from. A plain-code checker (`check_citations`) confirms that each quote really appears in that turn's transcript. If any quote is missing, the judge is asked once to fix it. Quotes that still fail are dropped, and a score left with no evidence becomes "insufficient_evidence". If the judge is unavailable, the report shows the checked claims and the moves but no scores at all, because an invented score is worse than none.

**Analogy.** The judge is an examiner who must staple a photocopy of your exact words to every mark they give. A clerk checks every staple against the answer sheet. The examiner gets one chance to fix a wrong staple, and any mark still without a valid staple is crossed out.

**How it is tested.** `tests/test_judge.py` checks the nine dimensions, the score range, the Hindi report option, the citation check, and the one-revision limit.

### 4.6 Opener (in `brain/actor.py`)

The first question comes from the application form (home state, district, degree subject, hobbies), the way a real board opens from the candidate's application. The form never asks for a name or any identifier. The opener runs on the actor's lane and is guarded in code: it must be an open-ended question ending with "?", and it must not state a fact about a place ("Kanpur is famous for…", "sabse bada…") or contain any number unless an existing, sourced fact card backs it. If the model's line breaks a rule, or the call fails or misses the time budget, a safe template replaces it: "Namaste, baithiye. Aap Kanpur Nagar se hain aur Electronics padha hai. Wahan rehte hue aapne kaunsi ek samasya dekhi, aur use kaise suljhayenge?"

**Analogy.** The chairperson glances at your form before the first question, but is not allowed to say "your district is famous for leather" unless a fact card on the desk proves it, because a wrong fact in the very first sentence would ruin the board's credibility.

**How it is tested.** `tests/test_actor_opener.py` feeds the guard bad openers (a place fact, a number, a yes-or-no question, no question at all) and checks that each is replaced, and that the template itself passes the guard.

### 4.7 Two building blocks every model call uses

**Forced tool use (`brain/llm.py`, `brain/tools.py`).** Nova Pro has no "JSON mode", so every structured call gives Nova exactly one tool whose input schema is the shape we want, and forces Nova to call it (`toolChoice` set to that tool), at temperature 0 with greedy decoding. The tool's input is our JSON. Pydantic validates it; if it is invalid, the call is retried once with the validation errors fed back to the model, and if it is still invalid, a typed error lets the node use its safe default. If the output was cut off at the token limit, the call is retried once with double the limit. Schemas are kept flat (only `type`, `properties` and `required` at the top level), because Nova's tool-call troubleshooting page lists other top-level fields as unsupported, and a code check refuses any schema that breaks this. This is ADR-004.

**Prompts and caching (`brain/prompts.py`, `prompts/`).** Every prompt is a versioned file, and every trace records the versions used. Each system prompt starts with the same shared prefix (shared rules, the persona pack and the fact cards), followed by a cache checkpoint, so Bedrock can reuse that prefix across calls. Today's synthetic prefix is only about 929 tokens by our rough estimate, below Nova's documented 1,000-token minimum per checkpoint, so zero cache hits are expected until real cards are added.

## 5. Three lanes and timeouts

boto3 is synchronous, so every Bedrock call holds a worker thread for its whole duration, and a streamed actor line holds one until its last word. With one shared pool of threads, a burst of background fact checks could make the actor wait for a free thread. So every call runs on one of three **lanes**, chosen from the call's name, and each lane has its own thread pool and its own boto3 client with its own timeouts (ADR-005):

- The **actor lane** carries the actor and the opener: the critical path. Defaults: 4 threads, 2-second connect timeout, 4-second read timeout, one attempt, and a 2.5-second first-token budget.
- The **fast lane** carries the listener, the only work between end of turn and the actor. Defaults: 4 threads, 2-second connect timeout, 5-second read timeout (a whole listener call must fit inside it), at most 2 attempts in total through the fast graph's retry policy, 0.1 seconds before the retry, and no jitter. The listener node also stops waiting after `fast_listener_timeout_s` (3 seconds), or as soon as `end_of_turn` has passed its deadline. It then records a fallback analysis and "parks" the still-running call; if the call returns later, its claims are still fact-checked, but its move is never used.
- The **background lane** carries the librarian and the judge. Defaults: 8 threads, 5-second connect timeout, 30-second read timeout, and 3 attempts with backoff and jitter.

The end-of-turn deadline is 1.5 seconds for the future voice path and 30 seconds in text mode (the candidate is typing). The 1.5 seconds is provisional: added to the actor and Timbre, it does not fit the 3.0-second p95 budget in §1, so the Prisma and Nova probes will set it. Every one of these numbers is UNVERIFIED and lives in `config.py`, to be set from the Nova probe's measurements from the EC2 server. A call name that is not listed goes to the background lane, so a new kind of call can never take a critical-path thread by accident.

**Analogy.** A shop with three checkout counters. A long queue of trolleys at the "background" counter never holds up the express counter. And each counter has an egg timer: when it rings, the cashier stops waiting for a card machine that has frozen.

**How it is tested.** `tests/test_lanes.py` runs the real Bedrock client over a fake boto3 whose calls really block their thread. It proves that a saturated background lane does not delay the listener or the actor, and, as a control that shows the test can fail, that the same load on one shared pool does delay them. `tests/test_timeouts.py` checks the timeouts and retry limits per lane, that a hung listener on segment 1 does not stop segment 2 or the next turn, and that a late listener result is still fact-checked.

## 6. Fallbacks and the chaos test

Every outside call has a written fallback, and every fallback is recorded in the trace. In plain words:

- If the listener fails, the segment gets a fallback analysis with no claims, so nothing can be challenged, and the strategist will not press a candidate on a guess.
- If a librarian call fails, the claim gets an "unverifiable" fallback vote, which is never cached and never challenged.
- If the strategist's code raised an error (it should not), the move becomes a neutral probe.
- If no move is ready by the end-of-turn deadline, the latest ready move or the silence-or-probe rule is used.
- If the actor fails or misses its budget, the persona fallback line or the move's template is spoken (ruling 14).
- If the opener fails, the opener template is spoken.
- If the judge fails, the report shows no scores rather than guessed ones.

**The chaos test** (`tests/test_chaos.py`) runs 24 complete interviews on the real Bedrock client over a fake boto3, and injects failures at random but reproducibly (each seed always injects the same faults): throttling errors, read timeouts and throttling in the middle of a stream, on about a quarter of the actor and fast-lane calls and 60 percent of background calls, plus listener calls that hang past their timeout. Half the seeds also use an end-of-turn deadline shorter than the listener timeout. For every interview it checks that the session finishes with a report; no fallback vote is ever cached; no fact challenge ever comes from a fallback verdict (checked against the fake's own record of which librarian calls really answered, not our code's flags); every fallback and every model call appears in the trace; and the actor is never retried, with each failed actor call speaking the right fallback line. A final check confirms the chaos really reached every lane and every fallback path, so the test cannot pass by accident. When the builder deliberately broke the persona fallback line, the test failed.

**Analogy.** A school fire drill. You do not wait for a real fire to find out whether everyone knows the way out; you ring the bell on purpose, many times, and check that every class reached the playground and that the register was taken.

## 7. Tracing

Every interview writes `traces/<session_id>.jsonl` (`src/mockboard/obs/trace.py`). It has one line when the session starts (card-set version, whether all cards are synthetic, feature flags, prompt versions, and how the opener went), one line per candidate turn, and one line for the report. A turn line records:

- the timestamp of every stage, in milliseconds since the turn started, such as `segment_0_submitted`, `s0.listener_end`, `end_of_turn`, `move_ready`, `actor_request` and `actor_first_token`;
- every model call, with its lane, latency, time to first token, attempts, token counts (including prompt-cache reads and writes), stop reason and error;
- the move, its rule and reason, the member, and the line spoken;
- how the move was reached: the deadline, how long `end_of_turn` waited (`move_wait_ms`), the fast graph's time per segment, and `fallback_reason` if a fallback move was used;
- the actor's outcome: `actor_guard`, `actor_fallback`, `actor_fallback_kind`, `actor_error`, `actor_fast_retry` (ruling 17) and `filler_played` (always false in text mode);
- this turn's claims and their verdicts so far, the fact checks that finished since the last line, late listener calls, analyses that finished after their move was chosen (`stale_analyses`), and the list of `fallbacks`.

Text mode records only the stages it really has. The voice stages (voice-activity end, first audio byte, playback start) will be added with the same `mark()` call when the voice loop exists, never faked. This file is the source of truth for every latency number we publish. A Langfuse hook exists behind `LANGFUSE_ENABLED`, off by default, and is only a stub today.

**Analogy.** An aeroplane's black box. It does not fly the plane, but after every flight you can replay exactly what happened, second by second, and why.

## 8. Evaluation

The evaluation (`eval/`) answers one question: does the board catch wrong facts without ever "correcting" right ones? Each item is a scripted answer to a question, labelled with its expected claims. The runner sends each item through the real fast and fact-check graphs as two turns: the scripted answer, then a neutral follow-up, so that a fact challenge (which always comes a turn late) has its chance.

The metrics, all reported as counts first:

- **Planted-error recall**: of the deliberately wrong claims, how many ended `contradicted` against the right card.
- **False corrections**: of the claims labelled true, how many ended `contradicted`, which means the board would have challenged a correct statement. This matters more than recall, because a board that wrongly corrects a candidate is worse than one that says nothing.
- **Move-class accuracy**: the nine moves are grouped into three classes (challenge, push back, follow up), and each item has an expected class.
- **Balanced pushback**: how often a balanced answer was pushed back on (it should be rare).
- **Claim F1**: how well the extracted claims match the expected claims one to one.
- **Fallbacks**: how many fallbacks fired during the run.

**Clopper–Pearson in one paragraph.** Suppose the board made zero false corrections on 150 true claims. That does not prove the rate is zero: with only 150 tries, a small rate could easily produce zero by luck. The Clopper–Pearson upper bound answers the question, "what is the highest true rate that could still reasonably produce a result this good?" It is computed exactly from the binomial distribution (the maths of repeated yes-or-no trials), rather than from the usual normal-curve shortcut, which is unreliable when counts are small and close to zero. Our code reports the upper end of the two-sided 95 percent interval. For zero out of 150 it gives about 2.4 percent, and for zero out of 30 about 11.6 percent. So a small set can only promise a loose bound, which is why the plan calls for about 150 hand-checked true claims.

**Dev and held-out split.** Every item belongs to either the development split or the held-out split. You tune prompts and rules while looking only at the dev split. The held-out split is run only at milestones (about November 3 and 7), and the runner refuses to run it unless you pass `--allow-heldout`. If you tuned against every item, the numbers would slowly flatter the system; the sealed set tells you how it does on answers it was not tuned on.

**Analogy.** The dev split is the practice paper you may look at as often as you like. The held-out split is the final exam paper, kept in a sealed envelope until exam day.

**The merge gate.** `make eval-gate` (`eval/gate.py`) turns the eval into a pass-or-fail check for any change to a prompt, a schema, a model setting or the strategist's rules. It runs the dev split and exits with a non-zero code when planted-error recall falls below `EVAL_GATE_MIN_PLANTED_RECALL` (0.8 by default, a placeholder until the golden set exists), when there are more false corrections than `EVAL_GATE_MAX_FALSE_CORRECTIONS` (0 by default, so any false correction fails), or when the items contain no planted error at all, because a gate that measures nothing must not pass. Given a committed baseline summary (`make eval-gate EVAL_BASELINE=...`), it also fails if the count of planted errors caught goes down or the count of false corrections goes up. It compares counts rather than percentages, because in a small set one item is worth several percentage points. `tests/test_eval_gate.py` proves each failure case, including a real process exiting with code 1.

**Honest status.** Today the set has six illustrative example items that cite synthetic test cards. The owner's real golden set (60 answers and about 150 hand-checked true claims) is not written yet, and with the fake model the numbers test the pipeline, not Nova. Run it with `make eval`.

## 9. Agentic patterns: what is used and what is not

Mock Board is a **deterministic, multi-stage workflow** that calls a language model at fixed points. It is not a set of autonomous agents, and the submission should say so plainly. These named patterns are used:

- **Prompt chaining.** The listener's typed output feeds the librarian, whose vote feeds the gate, whose verdict feeds the strategist, whose move feeds the actor. Each stage produces validated, typed data.
- **Router.** The strategist is a deterministic router over nine moves, and LangGraph's conditional edges route inside the graphs (skip the librarian when there are no claims; send the report back to the judge at most once).
- **Forced tool use for structured output.** Every structured call forces Nova to call one tool whose input is our JSON (ADR-004). Tools are used only for structured output, never for the model to act in the world.
- **Fan-out with a reducer.** The fact-check graph sends each claim to its own librarian branch with `Send`, and a reducer merges the votes.
- **Verifier gate.** The numeric check, the "card must exist" rule, the strategist's second check and the actor's guards all have to pass before a challenge is spoken (ADR-003).
- **Bounded evaluator–optimizer.** The judge drafts, a code checker verifies every quote, and the judge revises at most once, after the interview only.
- **Persona-conditioned generation.** There are three board members but a single actor call, conditioned on the speaking member's persona. The members do not talk to each other.

And these are deliberately **not** used:

- **No autonomous agent loop on the hot path.** There is no ReAct loop, no reflection step, and no model "supervisor" choosing the next step during the interview. Each extra model round trip from India to Sydney adds latency before the board can speak, and a model choosing its own path makes behaviour hard to reproduce and test. The strategist's rules do that job predictably.
- **No multi-agent debate.** Having the personas argue before speaking would multiply model calls for little benefit.
- **No search tool or vector database.** The librarian reads 40 to 60 cards per topic from the cached prompt (PLAN_v2_DELTA decision 6). It is not an agent that searches, and should not be described as one.
- **No long-term memory across sessions yet.** Memory is only the typed session state.
- **No LangGraph interrupts during a live session**, because there is no human to approve anything and a pause would stall the board. **No LangGraph node cache** for the librarian (ruling 10).

## 10. Reading order and commands

Read the code in this order; each file is short enough to read in one sitting:

1. `src/mockboard/brain/schemas.py`, for the data shapes (Claim, Analysis, BoardMove, SessionReport).
2. `src/mockboard/session.py`, for the three-call session API.
3. `src/mockboard/brain/graphs.py`, for the three graphs.
4. `src/mockboard/brain/listener.py`, `librarian.py` with `numbers.py`, and `strategist.py`, for the node logic.
5. `src/mockboard/brain/actor.py` and `spoken.py`, for the spoken line and its guards.
6. `src/mockboard/brain/judge.py`, for the report.
7. `src/mockboard/brain/llm.py`, for lanes, forced tool use and the fake client.
8. `src/mockboard/obs/trace.py` and `eval/runner.py` with `eval/metrics.py`.

To run one area's tests while you read it, on your laptop (WSL), in the `mockboard` folder (the strategist is shown; swap in any test file name from the sections above):

```bash
PYTHONPATH=src:. python3 -m pytest -q tests/test_strategist.py
```

To see everything working together, on your laptop (WSL), in the `mockboard` folder:

```bash
make cli-fake
make eval
make eval-gate
```

The decision records behind these choices are in `docs/adr/`, and the rulings they come from are in `docs/ORCHESTRATOR_LOG.md`.
