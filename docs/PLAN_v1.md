# Mock Board: Master Plan (draft v1, Oct 25 2026)

## Context
- Builder: solo B.Tech student (HBTU Kanpur, Tier 2 city), strong in cloud-native infra; 16 days to deadline.
- Contest: Gnani.ai "Great Indian AI Internship Challenge 2026". Theme "Build Sovereign AI for India". Open brief. Must use Gnani models/APIs and name them: Prisma v2.5 (STT, API), Timbre v2.5 (TTS, API), Evon v3.3 (30B MoE ~3.5B active, open weights only, not on API). Public demo on LinkedIn/X with hashtags is the entry point; engagement verified; shortlist; 1:1 interview. 100 paid internships; 10 awards (only one per team): Main Character (regional/low-resource language), Mixed Vibes (code-mixed), Real Deal (useful real-world), Noise Canceller (telephony), Two-Person Army, Solo Ninja, Jugaad Genius (creative/unexpected use), Small Town Big Build (Tier 2/3), Demo Day Drop (60-s video), Crowd Favourite. Synthetic data only (no real calls/identifiers). "A demo that fakes a result costs more than one that shows an honest limitation." Deadline Nov 10, 2026 11:59 PM IST, no extensions. 5,000 credits (₹1 each).
- Secondary audience: SuperKalam (YC-backed AI UPSC mentor).
- Constraints from the builder: AWS account; Bedrock only in Sydney (ap-southeast-2); only Amazon Nova Pro allowed as LLM; wants to deploy on EC2 (not a separate VPS provider). Gnani pricing: STT ₹27/hour (REST, WebSocket 20 concurrent sessions, batch); TTS ₹27 per 10,000 chars.

## Product
Mock Board: live Hinglish-first voice UPSC Personality Test board. 10-min session + feedback report.
Flow: mini application form (district, graduation subject, hobbies, language) -> 3 personas with distinct Timbre voices (Chairman, retired IPS officer, economist) -> board opens from form, moves to two topics (tourism, internal security) -> candidate can barge in -> delayed fact-challenges ("coming back to the 30% figure...") -> report (rubric scores, transcript with flagged claims + sources, composure moments, model balanced answer) -> practise again.
MVP: browser app; Hindi/Hinglish (+English toggle); 2 topic packs with curated fact corpus; listener, librarian, strategist, actor, judge; side-channel (response latency, speech rate, fillers, pitch steadiness); barge-in; delayed fact-challenges; report page; public demo mode with caps + invite code.
Stretch: more personas/cross-talk, more topics, regional-language interview, Evon + Nova 2 Sonic baselines, phone-call access at 8 kHz.

## Architecture: "fast mouth, slow brain"
Browser <-> (WebSocket over TLS) <-> App server on EC2 Mumbai (orchestrator) <-> Gnani Prisma WS (audio in, final transcripts out) ; Actor -> Gnani Timbre WS (3 voices) ; LLM calls -> Nova Pro (Bedrock Sydney, text only).
Inside server: Orchestrator (turn-taking, barge-in, state, topic word-boosting) -> Listener (Nova Pro, JSON tool output: stance, reasons, claims, balance, honest_unknown) -> Strategist (deterministic rules, Nova Pro tie-break) -> Actor (Nova Pro streamed, <=30 words, persona) -> Timbre. Librarian (async: SQLite FTS5 BM25 + alias matching over curated FactCards, Nova Pro judges entailment; no source => "unverifiable", never challenged) feeds strategist one turn later. Side-channel (local numpy/VAD/pitch) feeds strategist. Judge (Nova Pro) after session.
Design rules: strategist mostly rules (contradicted claim -> fact_challenge; one-sided -> counter/steelman; 3 strong answers -> move_on); no correction without source; actor short; personas as config; topic word boosting via Prisma update_settings.
Moves: probe, counter, steelman, fact_challenge, pressure, reassure, move_on, clarify (dialect fairness: ask to repeat when transcript looks garbled).

## Data model
CandidateTurn {turn_id, topic, transcript, stt{lang_code, segments, stt_latency_ms}, voice_signals{response_latency_s, speech_rate_wpm, filler_count, pitch_steadiness, was_interrupted_by_board}, analysis{stance, reasons[], claims[{id,text,verdict}], balance, honest_unknown}}
FactCard {id, topic, statement, aliases[], source_title, source_url, as_of}
BoardMove {move, target, member, pressure, reason}
SessionReport: 1-5 per dimension (factual accuracy, reasoning depth, balance, composure, honesty) with evidence quotes + turn ids.
State in memory during session; SQLite at end.

## Latency
Target: board starts speaking p50 1.5 s, p95 2.5 s after candidate stops.
Budget: end-of-turn decision 700-900 ms silence (own rule on top of Prisma VAD); Prisma final transcript ~300 ms (in flight); Mumbai->Sydney RTT ~150 ms (measure); Nova Pro first sentence 400-800 ms (measure); Timbre first byte <200 ms p95.
Techniques: analyse per segment while they talk; only actor on critical path; smart end-of-turn (900 ms silence, or 700 ms if sentence complete; >4 s pause -> reassure); latency masking with cached persona "Hmm."/"I see." if no first sentence in 1.2 s; barge-in (stop Timbre, flush browser buffer, log interruption); echo protection (browser AEC, headphones prompt, higher VAD threshold while board speaks); 10-min sessions due to Prisma 15-min cap + reconnect; per-turn stage timestamps.

## Verified platform facts (from docs, Oct 25 2026)
- Prisma WS wss://api.vachana.ai/stt/v3/stream returns FINAL transcripts per server-VAD segment only (no partials; pipecat note says final only today). Messages: connected, processing (speech end detected), transcript {text, audio_duration_ms, segment_id, segment_index, latency}, error, settings_updated. No word timestamps, no confidence.
- Exactly 1,024-byte PCM s16le mono frames at real-time cadence (32 ms @16k). Accepts 8k/16k/44.1k/48k (server resamples).
- Config via WS upgrade headers (x-api-key-id, lang_code, x-sample-rate, x-format verbatim|transcribe, VAD headers x-vad-threshold default 0.7, x-min-silence-ms default 500, x-min-speech-ms, x-min-segment-ms, x-max-speech-s). Browser WebSocket cannot set headers -> must proxy via backend.
- Session capped at 15 minutes.
- Word boosting: x-bias-list up to 100 single words letters only + x-bias-score; updatable mid-session via update_settings JSON frame.
- en-IN accepts Hindi-English mixed audio and outputs Latin script; hi-IN outputs Devanagari. Languages: bn, en, gu, hi, kn, ml, mr, pa, ta, te (-IN). No Awadhi/Bhojpuri.
- Timbre: WS wss://api.vachana.ai/api/v1/tts (interruptible), SSE /api/v1/tts/sse, REST; p95 first byte <200 ms; code-switch aware; voices like Pranav, sia, Karan. A pipecat-gnani plugin exists (Gnani-maintained) with STT/TTS services incl. InterruptibleTTSService; Pipecat also has AWS Bedrock LLM service.
- Nova Pro served in-Region in Sydney; Nova 2 Sonic (speech-to-speech, supports Hindi + Indian English) in us-east-1, us-west-2, Tokyo.

## Infra & security
EC2 t3.small Mumbai, Ubuntu; Caddy auto-HTTPS (mic needs HTTPS) on free DDNS or cheap domain; Python 3.11 FastAPI/uvicorn asyncio; boto3 ConverseStream to ap-southeast-2; SQLite (+ nightly S3 copy); static HTML/TS frontend with AudioWorklet; systemd or Docker Compose. IAM role limited to Nova Pro invoke + one SSM path; Gnani key in SSM SecureString; SSH closed (SSM Session Manager); WS origin check, short-lived token, 1 session/token, 10-min hard stop; candidate speech treated as data; AWS Budget alarm. GitHub + tests + offline eval on push; deploy = pull + restart.

## Cost
Per 10-min session: STT ~5 min ₹2.3; TTS ~3,000 chars ₹8.1; Nova Pro ~110k in / 8.5k out ≈ ₹10 (verify Sydney price); EC2 ~₹1,500/month. ~450 sessions on Gnani credits.
Levers: cache fixed lines; ≤30-word actor lines; send audio to Prisma only when local VAD hears speech; small actor prompt.
Abuse: public 3-min rapid round; invite code for full; daily global cap, 2/visitor/day; spend meter + kill switch; friendly limit page.

## Evaluation
Text set: 60 scripted answers (20 planted errors, 20 one-sided, 20 balanced+correct). Audio set: 30 clips by builder (10 Hindi, 10 Hinglish, 10 Awadhi-flavoured; same content) with reference transcripts. Live: 10 sessions incl. 3-5 consenting students + realism survey.
Metrics/targets: planted-error recall ≥80%; false corrections ≤2%; move-class match ≥85%; pushback on balanced <10%; claim extraction F1 ≥0.85; WER per style (report; compare hi-IN vs en-IN); fairness score gap ≤0.3/5; latency p50 1.5 s p95 2.5 s; barge-in stop <300 ms; realism ≥4/5; ₹/session.
Baselines: Evon v3.3 as listener (cloud GPU notebook); Nova 2 Sonic end-to-end if access.

## Fairness, safety, privacy
No accent/dialect/grammar dimension; clarify move for garbled transcripts; composure vs candidate's own baseline. Sensitive topics at policy level; no partisan judgements. No real identifiers; audio not stored by default; transcripts deletable; consent notice.

## Contest strategy
Submit Nov 9. Awards overlap: Solo Ninja (complete, evaluated), Jugaad Genius (side-channel, topic word boosting, delayed fact-challenges), Mixed Vibes (Hinglish), Main Character / Small Town (dialect-fair + Tier 2). Submission names Prisma, Timbre, Evon (eval), Nova Pro. 60-s demo script. LinkedIn + X posts with hashtags; build-in-public updates.

## Roadmap
Oct 25-26 foundations + 3 latency probes; Oct 26-30 text-only brain + text set; Oct 30-Nov 3 voice loop; Nov 2-4 side-channel; Nov 4-6 judge/report/demo mode deployed; Nov 6-8 eval + baselines; Nov 8 demo video; Nov 9 post + submit; Nov 10 buffer.
Cut order: Nova 2 Sonic baseline, Evon baseline, third persona, pitch, second topic. Never cut fact-checking with sources or the eval table.

## Risks
Nova Pro TTFT >1 s from Mumbai (measure day 1; masking; move EC2 to Sydney); scope slip; malformed JSON (tool-use schema, validate, retry, default move); Prisma splitting/early turn end; echo false barge-in; Awadhi WER; false corrections; credit drain; sensitive topic on camera; mic blocked without HTTPS.
