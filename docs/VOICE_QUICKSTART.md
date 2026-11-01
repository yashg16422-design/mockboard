# Voice quickstart: talk to the real board on your Mac

This page explains how to run Mock Board's voice app on your MacBook and speak to the real board. The app uses Gnani Prisma for speech-to-text, Amazon Nova Pro for the board's thinking, and Gnani Timbre for the board's voice. Everything runs on your laptop at `http://localhost:7860`. Chrome allows the microphone on `localhost` without HTTPS, so no certificate is needed (ruling 20).

Everything on this page that depends on the real Gnani or AWS services has been tested only against fakes. Your first real run is the first time it meets them, and the last section lists exactly what that run must check.

## 1. One-time setup

You need the repository's virtual environment (`.venv`, made by `make venv`), plus the pinned voice packages: Pipecat 1.5.0, the Gnani plugin and aiortc. The voice packages are an optional extra, so `make venv` does not install them.

On your Mac, in Terminal (zsh), from the `mockboard` folder:

```zsh
make venv            # skip this if .venv already exists
make voice-install   # the same as: .venv/bin/python -m pip install -e '.[voice]'
make test            # should pass, with no network and no credits
```

The first time Pipecat is imported, it tries to download a small NLTK data file. That needs internet once; offline it only logs an error and carries on.

Next, put your keys where the app can read them. The Gnani key goes in `.env` in the `mockboard` folder; it is never written into code or logs. AWS credentials come from your normal AWS CLI profile, never from `.env`.

On your Mac, in Terminal, from the `mockboard` folder:

```zsh
cp -n .env.example .env       # only if you have no .env yet
open -e .env                  # set GNANI_API_KEY=... and save
export AWS_PROFILE=default    # or the profile that has Nova Pro access in ap-southeast-2
aws sts get-caller-identity   # should print your account, not an error
```

If your `.env` was copied from an older `.env.example`, check two lines: `TTS_LANGUAGE` must be `auto` (ruling 25), and the new `STT_*` and `VOICE_*` lines can be copied over from `.env.example`. If a line is missing, the default in `config.py` is used.

## 2. First, a free run with fake speech and a fake model

This run checks the browser, the microphone, WebRTC, Silero (the voice detector) and Smart Turn (the end-of-turn detector) without spending anything. The board answers with beeps, and your words are replaced by scripted answers, but the turn-taking is real: you speak, pause, and the board "answers".

On your Mac, in Terminal, from the `mockboard` folder:

```zsh
make voice-fake
# the same as: .venv/bin/python -m mockboard.voice.server --mode upsc --fake-llm --fake-speech
```

Open `http://localhost:7860` in Chrome, put on headphones, fill in the form, press **Start** and allow the microphone. You should see the opener's captions and hear beeps. Speak a sentence and pause; the status changes to "you paused", then "the board is thinking", and a beep plays. Speak over a beep for about half a second; the beep should stop at once. Press **Stop**, or let the session end, to see the report. Press **Ctrl+C** in Terminal to stop the server.

## 3. The real UPSC board

On your Mac, in Terminal, from the `mockboard` folder:

```zsh
export AWS_PROFILE=default
make voice-upsc
# the same as: .venv/bin/python -m mockboard.voice.server --mode upsc
```

Open `http://localhost:7860` in Chrome, wear headphones, fill in the application form (it never asks for names, roll numbers, phone numbers or colleges) and press **Start**. The Chairperson opens, and you answer aloud. Captions of both sides appear live, along with the name of the board member who is speaking. When the interview ends, the report appears in the page and is saved as `traces/<session id>.report.txt` (plus `.report.json`). The turn-by-turn trace is `traces/<session id>.jsonl`.

## 4. The real debate

The board argues the other side of a curated motion. You pick a motion by its number (1 to 13; the list is in `data/motions/curated.json`, and `make cli-debate` prints it) and pick your side.

On your Mac, in Terminal, from the `mockboard` folder:

```zsh
export AWS_PROFILE=default
make voice-debate MOTION=1 SIDE=for
# the same as: .venv/bin/python -m mockboard.voice.server --mode debate --motion 1 --side for
```

Extra options are `--length quick|standard`, `--language hinglish|english|hindi` and `--opponent logician|wit`. The Moderator introduces the motion, you give your opening statement, the opponent opens, there is a clash round, and then come the closing statements. At the end the Moderator says "The board will confer" and reads the verdict, and the full debate report appears in the page.

Useful switches for either mode: `--fake-llm` (real voice, free model), `--fake-speech` (real model, no Gnani credits), `--voice Vikrant` (another voice of the model in `TTS_MODEL`), `--max-turns 4` (a shorter session), `--record-audio` (save your own microphone audio, section 7) and `--port 7862`.

## 5. What a session costs (estimates, not measurements)

- **Nova Pro (AWS).** A 10-minute UPSC session has about 9 board turns. Each turn makes one listener call per transcript segment and one actor call, and the session ends with the judge. That adds up to roughly 60,000 to 100,000 input tokens and 6,000 to 10,000 output tokens. At Nova Pro's published list price ($0.0008 per 1,000 input tokens and $0.0032 per 1,000 output tokens), that is about US$0.07 to US$0.12 per session. The Sydney (`ap-southeast-2`) price is unverified, so check the Bedrock pricing page.
- **Gnani (credits, ₹1 each, 4,900 in total).** Prisma receives your microphone for the whole session (up to 10 minutes, the hard stop set by `VOICE_SESSION_MAX_S`). Timbre speaks about 9 to 12 board lines, which is about 2,000 characters or 2 to 3 minutes of audio. Gnani's per-minute and per-character rates are not in our docs (unverified). Please note your credit balance in the Gnani dashboard before and after your first real session and send both numbers back, so we can work out how many sessions the credits allow.
- **A free run** (`make voice-fake`) costs nothing at all.

## 6. What to try (about 15 minutes in total)

1. **A normal UPSC session with headphones.** Answer naturally in Hinglish, including one long answer of 30 seconds or more.
2. **Pauses.** In one answer, stop mid-thought for one or two seconds and then continue. Note whether the board cut in. If it did not start speaking yet, the turn should simply continue (ruling 31); the page shows "you went on".
3. **Barge-in.** Talk over the board for a full sentence. It should stop within a fraction of a second. Then cough once while it speaks: a cough should *not* stop it.
4. **A wrong figure.** Say a clearly wrong number about tourism (for example, "tourism is 30 percent of GDP"). A turn or two later, the board should ask you about it as a question, never as an accusation.
5. **A debate.** For example, `make voice-debate MOTION=1 SIDE=for`, with one opening, one or two clash answers and a closing.
6. **Optional: without headphones, once.** Tell us whether the board interrupted itself.
7. **The transcription A/B (ruling 35).** Do one session with `make voice-record`, then run `make stt-ab` on that recording (section 7 below). It compares three Prisma settings on exactly the same audio, which is fairer than comparing two different sessions. Whether the contest submission may name Prisma v3.0 is unverified.
8. **The voice lab (ruling 38).** Pick the board's voice by ear with `make voice-lab` (section 8 below).

## 7. Recording your voice and comparing transcription settings (ruling 35)

The first real session showed that `en-IN` turns Hinglish into mangled Latin text. To choose a better Prisma setting, you record one session of your own voice and then replay the same recording through three settings, reading the transcripts side by side.

**Recording is off unless you ask for it.** With `--record-audio`, the app saves only your microphone audio (never the board's voice) on your Mac, as `traces/<session id>.mic.wav` (16 kHz mono, exactly what Prisma received). The page shows a yellow note while recording is on, and the Terminal prints where the file went when the session ends. Nothing is uploaded. Delete the file whenever you like.

On your Mac, in Terminal, from the `mockboard` folder:

```zsh
export AWS_PROFILE=default
make voice-record
# the same as: .venv/bin/python -m mockboard.voice.server --mode upsc --record-audio
```

Do a normal UPSC session in Hinglish (two or three minutes of your speech is enough), then press **Stop** and **Ctrl+C**.

Next, replay the recording through Prisma. The tool streams the whole recording once per setting, one setting after another, at real-time speed, so it takes about three times as long as the recording and spends about three recordings' worth of Prisma credits.

On your Mac, in Terminal, from the `mockboard` folder:

```zsh
make stt-ab                                   # the newest traces/*.mic.wav
make stt-ab WAV=traces/voice-abc123.mic.wav   # or a recording you name
# the same as: .venv/bin/python -m mockboard.voice.stt_ab traces/voice-abc123.mic.wav
# free check first (prints the headers, sends nothing):
.venv/bin/python -m mockboard.voice.stt_ab --dry-run
```

The three settings are `prisma-v2.5` with `en-IN` (what the first session used), `prisma-v2.5` with `hi-IN`, and `prisma-v3.0` with `language_code: hi-IN,en-IN` and `format=codemix` (Hindi in Devanagari, English words kept in Latin). For each setting, the output shows the full transcript, a timing line (how long each final took after Prisma's `processing` message, and Prisma's own `latency` field) and the detected languages, followed by a table aligned by the second of audio where each segment ended. The same table is saved next to the recording as `<recording>.stt_ab.md`. The transcripts contain your words, so read them yourself and send back only your choice and the timing lines unless you are happy to share the text.

To use the winner, set the matching lines in `.env` (for example `STT_MODEL=prisma-v3.0`, `STT_LANG_CODE=hi-IN,en-IN`, `STT_FORMAT=codemix`).

## 8. Choosing the board's voice in the voice lab (ruling 38)

Gnani's TTS documentation (read on October 31, 2026) lists two models: `timbre-v2.5`, which the board uses today, and `timbre-v3.0`, which Gnani announces as "Expressive text-to-speech". Every v3.0 voice speaks all ten languages, and voices are not shared between the models. The voice lab is a local page that plays the same three board lines (the Chairperson's opener, a topic question and a debate rebuttal) with different settings, so you can choose by ear.

First, check the page for free. It plays tones instead of voices. On your Mac, in Terminal, from the `mockboard` folder:

```zsh
make voice-lab-fake
# the same as: .venv/bin/python -m mockboard.voice.lab --fake
```

Then use the real voices. On your Mac, in Terminal, from the `mockboard` folder:

```zsh
make voice-lab
# the same as: .venv/bin/python -m mockboard.voice.lab
```

Open `http://localhost:7861` in Chrome and wear headphones. Each table changes one thing from the baseline, which starts at what the board sends today (Poorvi, v2.5, `auto`, Latin script, one request per sentence, 16 kHz). The tables cover:

- six voices across both models: Poorvi, Vikrant and Urmila (v2.5), and Avinash, Yashraj and Vasudha (v3.0);
- the language field: `auto`, `hi-IN`, `hi-en` (Hinglish) and `en-IN`;
- the script: Latin, Devanagari with the English words in Latin, and all Devanagari;
- one request for the whole line versus one request per sentence;
- 16 kHz versus 24 kHz.

Nothing is rendered until you press a play button. Each new render sends that line to Timbre once (the page counts the characters sent, since Timbre bills per character at a rate we have not verified). Every render is saved in `traces/voice_lab/`, so playing it again, even after a restart, costs nothing. You can change the baseline at the top of the page to compare around another voice.

Under each row, the grey line shows the `.env` lines that make the board sound like that row, for example:

```
TTS_MODEL=timbre-v3.0
TTS_VOICE=Yashraj
TTS_LANGUAGE=hi-IN
TTS_SAMPLE_RATE=24000
TTS_REQUEST_UNIT=line
```

Two limits apply. First, the board cannot send `hi-en` yet, because ruling 25 keeps the board to languages the Gnani SDK also accepts; if `hi-en` clearly wins, tell us and the orchestrator will rule on it. Second, the script (Latin or Devanagari) is decided by the board's prompts, not by a setting, so if Devanagari clearly wins, tell us that too.

`TTS_REQUEST_UNIT` has three values. `sentence` (the default) sends each sentence as soon as the board writes it, which gives the fastest first sound. `line` waits for the whole line and sends it as one request, which may sound more natural but starts later, by roughly the time the board takes to write the rest of the line. `first_rest` sends the first sentence at once and the rest of the line as one request, which is a middle ground. After changing it, run one session and check **STOP->FIRST AUDIO** in `make voice-summary`, which now also prints the TTS settings the session used.

## 9. How to send the results back

After each session, run the summary. It prints timings and counts only: no transcript text, no board lines and no keys, so it is safe to paste.

On your Mac, in Terminal, from the `mockboard` folder:

```zsh
make voice-summary
# or: .venv/bin/python -m mockboard.voice.summarize --all    (every voice session in traces/)
```

Paste the output into the chat, together with:

- your Gnani credit balance before and after;
- what you heard: whether the voice sounded right with `TTS_LANGUAGE=auto`, whether there were false cut-ins and how many, how barge-in felt, and whether there were any long silences;
- any red error line from the page, and the last 30 lines of the Terminal output if something broke (keys are never printed there).

The key numbers in the summary are **STOP->FIRST AUDIO** (from the end of your speech to the board's first sound; our target is a p95 under 3 seconds), the **commit** replies and waits, and the barge-in and reopen counts.

## 10. What still needs a real run (all UNVERIFIED until then)

- **Prisma.** That the connection is accepted with the new header names (`language_code`, `format`, `model`, `x-idle-timeout-sec`, `x-api-request-id` = `<session id>-c1`). That 1,024-byte frames work. That `speech_start` arrives, which tells the app whether an utterance is open. That `commit` closes an utterance quickly, and what it does when nothing is open. That `connected` confirms the model that served.
- **Turn-taking.** How often Smart Turn ends a Hinglish turn too early or waits its full 3 seconds, and whether the barge-in threshold (`VOICE_BARGE_IN_MIN_SPEECH_MS=500`) and the resume threshold (`VOICE_RESUME_MIN_SPEECH_MS=150`) feel right.
- **Timbre v3.0 and the new settings (ruling 38).** That `timbre-v3.0` is accepted with a v3.0 voice and a `language` field (the Gnani SDK we use predates v3.0, so our code builds the request itself), that a whole line in one request is accepted and how much later it starts, that 24 kHz output plays correctly through WebRTC, and that the voice lab's renders work.
- **The A/B tool (ruling 35).** That Prisma accepts each of the three settings and that real-time replay of a recording gives the same kind of transcript as a live session.
- **Timbre.** That one-request-at-a-time works, that the first byte arrives within `TTS_FIRST_BYTE_TIMEOUT_S` (3 s), that a barge-in silences Poorvi at once, and that `auto` voices Latin-script Hinglish acceptably.
- **Nova Pro.** The real latencies: `turn_end->move` (the listener) and `move->1st sentence` (the actor). These decide `MOVE_DEADLINE_MS` (still 1,500, a placeholder).
- **WebRTC.** That Chrome on macOS connects to `localhost` without STUN servers.
