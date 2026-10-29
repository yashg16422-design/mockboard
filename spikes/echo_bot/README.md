# V1 echo spike: hear your own words back through Pipecat and Gnani

This is throwaway code for voice chunk V1 (CTO_NOTES_03 §3). You speak into Chrome, Silero notices when you stop, Smart Turn v3 decides your turn is over, Gnani Prisma writes down what you said, and Gnani Timbre (voice Poorvi) says it back to you. Every frame that moves through the pipeline is written to a log file, so we learn the real order of events in pipecat-ai 1.5.0. The real class and frame names it uses, and the surprises found while building it, are in [`spikes/NOTES.md`](../NOTES.md).

It runs on your Mac only, on `localhost`. Chrome allows the microphone on `localhost` without HTTPS, so no certificate or server is needed (ruling 20).

## Files

- `bot.py` builds the pipeline: the echo processor and the JSONL frame log. Since V2 it uses the app's own speech services from `src/mockboard/speech/`: `PrismaSTTService` (our Prisma client, ruling 24) and `TimbreTTSService` (the Gnani plugin plus the barge-in fix, ruling 25). The plugin's own classes are one flag away, for comparison (see below).
- `server.py` is a small web server. It serves the page and the WebRTC "offer" endpoint that the page calls to connect.
- `index.html` is the page: a Start button, a microphone level bar, and a short connection log.
- `fakes.py` holds a fake Prisma and a fake Timbre (a beep), used by `--fake-speech` and by the offline test in `tests/voice/`.
- `summarize.py` prints a readable per-turn timeline from a log file.
- `logs/` is where each run's log file goes. Git ignores it, because the logs contain what you said.

## One-time setup

You need the `.venv` that `make venv` created, and your Gnani key. The voice extra installs Pipecat 1.5.0, the Gnani plugin, aiortc (WebRTC), FastAPI and uvicorn into that same `.venv`. It does not change any of the packages the brain already uses.

On your Mac, in the mockboard folder:

```zsh
source .venv/bin/activate
python -m pip install -e '.[voice]'
```

The quotes around `.[voice]` matter in zsh, because zsh treats square brackets as a filename pattern. The install takes a few minutes the first time, because aiortc, PyAV, OpenCV and onnxruntime are large.

Next, make sure your Gnani key is in the `.env` file at the top of the repository. If you already ran the Prisma or Timbre probes, it is there. Otherwise, on your Mac, in the mockboard folder:

```zsh
cp -n .env.example .env
open -e .env
```

`cp -n` copies the example only if `.env` does not exist yet, and `open -e` opens it in TextEdit. Replace `replace-me-with-your-gnani-key` with your real key on the `GNANI_API_KEY=` line and save. The key never goes into code or logs.

If your `.env` is older than October 30, 2026, it probably has the line `TTS_LANGUAGE=hi-en`. The Gnani SDK refuses that value, and the spike would stop with an error naming ruling 25. In TextEdit, change that line to `TTS_LANGUAGE=auto` and save.

## Step 1: a free check with fake speech (no Gnani, no credits)

This checks the parts that do not depend on Gnani: the browser microphone, WebRTC, Silero and Smart Turn. Every time you pause, the bot answers with a short beep instead of your words.

On your Mac, in the mockboard folder, with the `.venv` active:

```zsh
python -m spikes.echo_bot.server --fake-speech
```

Then:

1. Plug in headphones. Without them, the bot's own voice can reach your microphone and interrupt itself.
2. Open http://localhost:7860 in Chrome and press **Start**.
3. Allow the microphone when Chrome asks. If macOS has never allowed Chrome to use the microphone, it asks too (System Settings, Privacy & Security, Microphone). If macOS asks whether Python may accept incoming connections, choose Allow, because WebRTC audio arrives over UDP.
4. The status should change to `connected`, and the Mic bar should move when you talk.
5. Say one sentence, for example "Good morning, I am from Kanpur", and then stay quiet.

**What to expect:** a short beep within about half a second of stopping if Smart Turn thinks you finished, or about three seconds later if it thinks you might continue. Both are normal; the log shows which happened. If there is no beep at all, look at the terminal for errors, then run the summary (below) and check whether "Silero: speech started" appears.

## Step 2: the real echo with Gnani (spends credits)

The pipeline streams microphone audio to Prisma the whole time the page is connected, silences included, and Prisma may charge for all of it (probe rule P7 has not settled this yet). Timbre charges for the characters spoken back. Keep the session short: five or six sentences, then press Stop.

On your Mac, in the mockboard folder, with the `.venv` active:

```zsh
python -m spikes.echo_bot.server
```

Open http://localhost:7860 in Chrome, press **Start**, and say these, pausing after each:

1. "Good morning. My name is Yash and I am from Kanpur."
2. "I studied electronics engineering, and my hobby is reading history."
3. A Hinglish sentence, for example "Mera optional subject sociology hai, kyunki mujhe society samajhna accha lagta hai."
4. Something long, and while the bot is saying it back, start talking again. This is the barge-in test.

**What to expect:**

- In the terminal, a line `[prisma] connected request_id=mb_...` when the page connects. If you see `Prisma rejected the API key` or `Prisma refused the headers (1008)` instead, stop and send the builder that line; it never contains your key.
- After each pause, Poorvi's voice says your sentence back. Prisma's `en-IN` model writes Hinglish in Latin script, so the Hindi words are read in Poorvi's Hinglish voice. How long the reply takes depends on Smart Turn and on when Prisma sends its final transcript; the summary shows the real numbers.
- In the barge-in test, the bot should go quiet within a fraction of a second of you starting to speak.

## Step 3: look at what happened

On your Mac, in the mockboard folder, with the `.venv` active:

```zsh
python -m spikes.echo_bot.summarize
```

It prints the newest log as a timeline in milliseconds: when Silero heard you stop, when each Prisma final arrived (with Prisma's own latency fields), when Smart Turn ended the turn, when the echo's audio reached the output, and any errors. The raw log is the newest `spikes/echo_bot/logs/echo_*.jsonl` file.

## Our STT or the stock one

By default the spike sends your voice to Prisma through **our** `PrismaSTTService`: exact 1,024-byte frames, one every 32 ms (what Gnani's documentation asks for), our headers (`lang_code`, `x-format`, `x-max-speech-s`, and the VAD values if you set them), automatic reconnects, and a `prisma_processing` line in the log each time Prisma closes a speech segment. Adding `--stock-stt` switches to the plugin's own `GnaniSTTService`, which forwards Pipecat's 20 ms (640-byte) frames as they are and sends none of our VAD headers.

To compare them, do the same three sentences once with each. On your Mac, in the mockboard folder, with the `.venv` active, first ours:

```zsh
python -m spikes.echo_bot.server
```

then, after pressing Stop and Ctrl+C, the stock one:

```zsh
python -m spikes.echo_bot.server --stock-stt
```

After each run, `python -m spikes.echo_bot.summarize` shows the timeline. Compare three things: whether every sentence came back at all (if the stock run returns nothing or garbled text, Prisma does not accept 640-byte frames), the `latency` field on each "Prisma final" line, and the gap between "Silero: speech stopped" and "Prisma final". Our run also shows "Prisma: segment ended, transcript pending" lines, which the stock one cannot.

`--stock-tts` does the same for speech output: the plugin's own `GnaniTTSService` instead of our `TimbreTTSService`. With it, a barge-in is expected to leave the old line playing.

To try Prisma's voice-detection settings with our STT (they are ignored with `--stock-stt`):

```zsh
python -m spikes.echo_bot.server --min-silence-ms 300
python -m spikes.echo_bot.server --vad-threshold 0.5 --max-speech-s 8
```

## Optional comparison runs

Each of these is one extra short run. Each flag switches off one of the spike's fixes or changes one default, so we can see what the plugin does on its own.

| Command (on your Mac, in the mockboard folder) | What it shows |
|---|---|
| `python -m spikes.echo_bot.server --stock-tts` | The plugin's own barge-in. The interrupted line is expected to keep playing (NOTES.md). |
| `python -m spikes.echo_bot.server --stock-stt` | The plugin's own STT: whether Prisma accepts Pipecat's 640-byte audio frames, instead of the 1,024-byte frames its documentation requires. |
| `python -m spikes.echo_bot.server --vad-start-only` | Only Silero may start your turn. A late Prisma final then cannot interrupt the bot. |
| `python -m spikes.echo_bot.server --lang hi-IN` | Prisma's Hindi model (Devanagari output) instead of `en-IN`. |
| `python -m spikes.echo_bot.server --tts-language hi-IN` | Timbre's Hindi language setting instead of `auto` (the new `TTS_LANGUAGE` default, ruling 25; the Gnani SDK refuses `hi-en`). Also try `en-IN`, or `none` to send no language. Listen for how the Hindi words in Latin script are pronounced. |
| `python -m spikes.echo_bot.server --log-audio-frames` | Logs every audio frame, not only the first of each burst. Use it with `--stock-tts` to count stale audio after a barge-in. |

Use `--port 7870` if port 7860 is busy, and `--voice Vikrant` (or any Timbre v2.5 voice) to try another voice.

## How to stop

Press **Stop** on the page, then press Ctrl+C in the terminal. Closing the tab also ends the session; the server notices the disconnect and stops that session's pipeline.

## Things that are UNVERIFIED

Nothing in this spike has talked to Gnani yet, and no human has spoken into it. The builder could only run it headless: an aiortc client played a synthetic (flite) voice clip into the server with `--fake-speech`, and offline tests used fake sockets. These items need your run:

- that Prisma and Timbre connect and respond through the plugin with your key;
- that our 1,024-byte framing and headers are accepted (and whether the plugin's 640-byte frames are too, with `--stock-stt`);
- how long Prisma's final takes after you stop, and whether it ever lands after Smart Turn has ended the turn (the log line `transcript_outside_turn`);
- that barge-in stops Timbre's audio in a real browser;
- how `auto`, `hi-IN` and `en-IN` sound for Hinglish (`--tts-language`);
- that Chrome on macOS connects with no STUN server on `localhost`.
