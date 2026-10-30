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

Useful switches for either mode: `--fake-llm` (real voice, free model), `--fake-speech` (real model, no Gnani credits), `--voice Vikrant` (another Timbre v2.5 voice), `--max-turns 4` (a shorter session) and `--port 7861`.

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
7. **Optional: the Prisma v3.0 A/B (ruling 31).** Add `STT_MODEL=prisma-v3.0`, `STT_LANG_CODE=hi-IN,en-IN` and `STT_FORMAT=codemix` to `.env`, run one UPSC session, then remove the three lines. Compare the caption quality on your Hinglish with the v2.5 run. Whether the contest submission may name v3.0 is unverified.

## 7. How to send the results back

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

## 8. What your first real run verifies (all UNVERIFIED until then)

- **Prisma.** That the connection is accepted with the new header names (`language_code`, `format`, `model`, `x-idle-timeout-sec`, `x-api-request-id` = `<session id>-c1`). That 1,024-byte frames work. That `speech_start` arrives, which tells the app whether an utterance is open. That `commit` closes an utterance quickly, and what it does when nothing is open. That `connected` confirms the model that served.
- **Turn-taking.** How often Smart Turn ends a Hinglish turn too early or waits its full 3 seconds, and whether the barge-in threshold (`VOICE_BARGE_IN_MIN_SPEECH_MS=500`) and the resume threshold (`VOICE_RESUME_MIN_SPEECH_MS=150`) feel right.
- **Timbre.** That one-request-at-a-time works, that the first byte arrives within `TTS_FIRST_BYTE_TIMEOUT_S` (3 s), that a barge-in silences Poorvi at once, and that `auto` voices Latin-script Hinglish acceptably.
- **Nova Pro.** The real latencies: `turn_end->move` (the listener) and `move->1st sentence` (the actor). These decide `MOVE_DEADLINE_MS` (still 1,500, a placeholder).
- **WebRTC.** That Chrome on macOS connects to `localhost` without STUN servers.
