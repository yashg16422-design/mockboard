# Probes

These scripts measure the three outside services Mock Board depends on before we build on top of them: Gnani Prisma v2.5 for speech-to-text, Gnani Timbre v2.5 for text-to-speech, and Amazon Nova Pro on Bedrock in Sydney for the language model. Each probe prints what it sees as it happens, and then appends a dated results table to `probes/results/RESULTS.md`, so that the numbers we quote later have a record behind them.

The Gnani probes spend real credits (4,900 in total, at one rupee each), and the Nova probe costs real AWS money. So always run the offline self-test first, then start with short runs.

## Files

- `gnani_ws.py` holds every Gnani wire-protocol detail in one place: the Prisma URL and upgrade headers (including the VAD headers, `x-max-speech-s` and the undocumented `x-partials`), the 1,024-byte framing at real-time cadence, the `update_settings` frame, the Timbre request body shared by WebSocket and SSE, the parsers for their replies, and Gnani's published voice catalog. Anything Gnani's docs leave unclear is marked `# UNVERIFIED:` in the code.
- `common.py` reads and writes WAV files, computes percentiles, and writes the results tables.
- `prisma_probe.py`, `timbre_probe.py` and `nova_probe.py` are the three probes, described below.
- `selftest.py` checks all of the above offline, against fake servers, without spending anything.
- `results/RESULTS.md` collects the result tables. `results/audio/` and `results/*.jsonl` hold raw outputs and are git-ignored.

## One-time setup

The probes read settings (URLs, region, model id, defaults) from the app's own config in `src/mockboard/config.py`, which reads environment variables and the `.env` file. The Gnani key must be in `GNANI_API_KEY`. AWS access uses the normal AWS credential chain, so nothing AWS-related goes in `.env`.

On your laptop (WSL), in the `mockboard` folder:

```bash
pip install -r requirements.txt
pip install numpy scipy        # only needed for --resample and the speech-length check
cp .env.example .env           # then edit .env and put your real Gnani key in GNANI_API_KEY
aws configure                  # once, if you have not set up an AWS profile yet
```

## Step 1: run the offline self-test

The self-test builds headers, frames audio, parses doc-shaped messages, runs a full Prisma session and a full Timbre session against small fake servers on your own machine, checks the Nova timing and JSON-validation code against fake responses, and validates the fact-card files. It makes no network calls and spends no credits. If anything fails, fix it before spending credits.

On your laptop (WSL), in the `mockboard` folder:

```bash
python -m probes.selftest
```

It should end with a line saying that all checks passed.

## Step 2: record a test WAV

Prisma wants 16 kHz, mono, 16-bit audio. Record about 20 to 30 seconds of yourself answering an interview question in Hinglish, with a couple of natural pauses and a filler or two ("umm", "matlab"), so the probe can show how segments and fillers come out.

To record from the microphone with `arecord` (part of `alsa-utils`). This works on native Linux; on WSL it needs WSLg audio, and if it fails, record on Windows instead and convert with `ffmpeg` as shown next. On your laptop, in the `mockboard` folder:

```bash
mkdir -p probes/results/audio
arecord -f S16_LE -r 16000 -c 1 -d 30 probes/results/audio/answer16k.wav
```

To convert any existing recording (for example an `.m4a` from your phone or a Windows Voice Recorder file copied into WSL) with `ffmpeg`. On your laptop (WSL), in the `mockboard` folder:

```bash
ffmpeg -i /mnt/c/Users/<you>/Downloads/answer.m4a -ac 1 -ar 16000 -sample_fmt s16 probes/results/audio/answer16k.wav
```

The Prisma probe refuses any WAV that is not 16 kHz mono 16-bit and prints the `ffmpeg` command to fix it. Passing `--resample` instead makes the probe convert stereo or other sample rates itself (the file must still be 16-bit).

## Step 3: Prisma STT probe

This probe streams the WAV to Prisma in 1,024-byte frames, one every 32 milliseconds, which is real-time speed, exactly as Gnani's docs require. Halfway through, it sends an `update_settings` frame that boosts a few words, and checks that a `settings_updated` reply comes back. After the clip ends it sends 1.5 seconds of silence so the server's voice detector closes the last segment, then waits until the server has been quiet for 6 seconds.

By default it runs the clip twice: once without the `x-partials` header and once with `x-partials: true`. That header is mentioned only once in Gnani's docs and never defined, so the comparison tells us whether interim transcripts exist at all. Each run therefore spends credits for the clip length twice. Use `--partials off` to run once.

For each run it prints every server message with its arrival time and saves the same log to `probes/results/prisma_<time>_<mode>.jsonl`. The summary reports:

- the number of final segments, and how many partial or interim messages arrived;
- transcript latency measured three ways: the server's own `latency` field, the gap between our receiving `processing` and `transcript`, and the time from sending the last audio frame to receiving the final transcript (closest to what a candidate feels);
- the raw concatenated text, so you can check by eye whether verbatim mode keeps the fillers;
- the sum of the server's `audio_duration_ms` next to a rough speech length we measure ourselves with an energy gate.

On your laptop (WSL), in the `mockboard` folder:

```bash
python -m probes.prisma_probe probes/results/audio/answer16k.wav
```

Useful options are `--lang hi-IN` (Devanagari output instead of Latin script), `--partials off|on|both`, `--min-silence-ms 800`, `--vad-threshold 0.5`, `--min-speech-ms 100`, `--min-segment-ms 120`, `--max-speech-s 15`, `--format transcribe`, `--bias-words "UPSC,Naxalism"` and `--bias-score 1.5`. Run `python -m probes.prisma_probe --help` for the full list. The VAD values are checked against the ranges in Gnani's docs before anything is sent. Be careful with `--min-segment-ms`: the docs warn that setting it too high silently drops short answers.

## Step 4: Timbre TTS probe

This probe synthesises one short Hinglish board line with each voice you name. For each voice it opens a WebSocket and sends the line twice on the same connection (the first "cold" request includes the connection handshake; the second "warm" request does not), and then sends the same line over SSE, which is the documented fallback. It reports connection time, time to the first audio byte, total time until the `complete` message, the number of chunks and the length of audio received. It asks for raw headerless 16-bit PCM at 16 kHz and saves each result as a WAV in `probes/results/audio/` so you can listen to the voices before choosing them.

Voice names must come from Gnani's voice catalog (docs.gnani.ai/api/TTS/available-voices.md). The probe checks every name against the catalog before spending credits, and refuses names that are not in it. The persona files already list candidates taken from that catalog: Vikrant and Omkar for the Chairman, Deepak and Hemraj for the IPS officer, and Poorvi and Nalini for the economist. Poorvi is the only Timbre v2.5 voice the catalog lists with Hinglish as its preferred language.

Each run spends credits for the text length times the number of requests. The default is three requests per voice (cold, warm and SSE). On your laptop (WSL), in the `mockboard` folder:

```bash
python -m probes.timbre_probe Poorvi Vikrant Deepak
```

Useful options are `--text "..."` (write numbers as words, as the docs ask), `--language hi-en|hi-IN|en-IN`, `--warm-repeats 0`, `--skip-ws` and `--skip-sse`.

## Step 5: Nova Pro probe

This probe makes N calls (50 by default) to Nova Pro through ConverseStream in `ap-southeast-2`, and measures time to first token and total latency for each, reporting the median (p50) and 95th percentile (p95). It then makes N more calls using forced tool use, built by the app's own request builder in `src/mockboard/brain/llm.py` so that the probe measures exactly the request shape the app sends, and checks each answer against a small Pydantic schema. The table shows how many answers were valid JSON, how many broke the schema, and how many came back with no tool call at all.

Both phases put a synthetic system prompt of about 1,500 tokens behind a cache checkpoint and add up the cache read and write tokens. Non-zero cache reads mean prompt caching is working. In the tool phase, they also answer an open question flagged in `llm.py`: whether caching still works when a tool is attached.

Throttling errors are retried with exponential backoff and random jitter, and every throttle is counted in the table. Errors that retrying cannot fix (missing credentials, access denied, an invalid request) stop the phase straight away instead of repeating N times.

Try a short run first, then the full one. On your laptop (WSL), in the `mockboard` folder:

```bash
python -m probes.nova_probe -n 3
python -m probes.nova_probe
```

To measure from where the app will actually run, repeat it on the server. On EC2, in `/opt/mockboard` (the instance role supplies the credentials):

```bash
python -m probes.nova_probe
```

Useful options are `--no-cache`, `--skip-stream`, `--skip-tool`, `--gap-s 0.5` (wait longer between calls if you see many throttles) and `--no-results` (do not write to RESULTS.md).

## Reading the results

Each probe adds a section to `probes/results/RESULTS.md` headed with the probe name and the date and time, followed by the parameters used, a results table and short notes (for Prisma, the raw transcript text). Remember that latency depends heavily on where you run from: numbers taken on a home connection in India will differ from numbers taken on the EC2 machine in Mumbai, so write down which machine you used in the notes.
