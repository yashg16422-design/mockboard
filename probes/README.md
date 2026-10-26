# Probes

These scripts measure the three outside services Mock Board depends on before we build on top of them: Gnani Prisma v2.5 for speech-to-text, Gnani Timbre v2.5 for text-to-speech, and Amazon Nova Pro on Bedrock in Sydney for the language model. Each probe prints what it sees as it happens, and then appends a dated results table to `probes/results/RESULTS.md`, so that the numbers we quote later have a record behind them.

The Gnani probes spend real credits (4,900 in total, at one rupee each), and the Nova probe costs real AWS money. So always run the offline self-test first, then start with short runs.

## Files

- `gnani_ws.py` holds every Gnani wire-protocol detail in one place: the Prisma URL and upgrade headers (including the VAD headers, `x-max-speech-s` and the undocumented `x-partials`), the 1,024-byte framing at real-time cadence, the `update_settings` frame, the Timbre request body shared by WebSocket and SSE, the parsers for their replies, and Gnani's published voice catalog. Anything Gnani's docs leave unclear is marked `# UNVERIFIED:` in the code.
- `common.py` reads and writes WAV files, computes percentiles, names the origin host, and writes the results tables.
- `prisma_probe.py`, `timbre_probe.py` and `nova_probe.py` are the three probes, described below.
- `selftest.py` checks all of the above offline, against fake servers, without spending anything.
- `results/RESULTS.md` collects the result tables. `results/audio/` and `results/*.jsonl` hold raw outputs and are git-ignored.

## One-time setup

The probes read settings (URLs, region, model id, defaults) from the app's own config in `src/mockboard/config.py`, which reads environment variables and the `.env` file. The Gnani key must be in `GNANI_API_KEY`. AWS access uses the normal AWS credential chain, so nothing AWS-related goes in `.env`.

On your laptop (WSL), in the `mockboard` folder:

```bash
pip install -r requirements.txt
pip install numpy scipy        # only needed for --resample and the speech-length check
pip install -e . --no-deps     # makes the app importable, which the probes need
cp .env.example .env           # then edit .env and put your real Gnani key in GNANI_API_KEY
aws configure --profile mockboard   # once, if you have not set up an AWS profile yet
```

On the EC2 instance in Mumbai (`ap-south-1`), the repository lives in `/opt/mockboard`. The instance needs an IAM role that allows `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on Nova Pro in `ap-southeast-2`; no AWS keys go on the machine. On EC2, in `/opt/mockboard`:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install numpy scipy        # the self-test imports numpy
pip install -e . --no-deps
aws sts get-caller-identity    # should print the instance role, proving credentials work
```

## Step 1: run the offline self-test

The self-test builds headers, frames audio, parses doc-shaped messages, runs a full Prisma session and a full Timbre session against small fake servers on your own machine, checks the Nova timing and JSON-validation code against fake responses, runs a whole `--app-prompts` run against a fake Bedrock client (checking that the requests are the app's real ones), and validates the fact-card files. It makes no network calls and spends no credits. If anything fails, fix it before spending credits.

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

The Nova probe has two modes. Both call Nova Pro on Bedrock in `ap-southeast-2` (Sydney), whichever machine they run from, and both append a table to `probes/results/RESULTS.md` whose heading names the mode, the origin machine and the region, for example "Nova Pro probe (app-prompts) from ec2-mumbai to ap-southeast-2".

### Mode 1: `--app-prompts` (run this one first; it gives the numbers the decision rules need)

This mode sends the app's real requests, built by the app's own code, so the probe measures exactly what a live interview would send. The system prompt is the real cached prefix (the persona pack plus the fact cards, followed by a cache checkpoint) and then the role's instructions.

- **Listener calls.** Each one is a forced-tool Converse call built by `BedrockNovaClient.build_tool_request` with the listener prompt and `LISTENER_TOOL`, exactly as `listener.analyse_segment` sends it. The answer is checked with the app's own `ListenerOutput` model, and the probe counts valid answers, schema-invalid answers, answers with no tool call, and answers cut off at `maxTokens`.
- **Actor calls.** Each one is a streamed ConverseStream call built by `BedrockNovaClient.build_stream_request` with the actor prompt and `build_actor_user`, exactly as `actor.compose_line` sends it. The probe cycles through seven realistic board moves: probe, counter, fact challenge, an acknowledgement after a conceded error, move on, steelman and reassure.

The sample turns are Hinglish, and every figure in them is about the fictional Exampleland, so no real statistic is sent or quoted. For every call the probe records the time to the first token and to the end of the first sentence (actor only), the total time, the input, output, cache-read and cache-write tokens, and every throttle together with how quickly it came back. The table also counts how many listener calls ran past `MOVE_DEADLINE_MS` or the fast lane's read timeout, and how many actor calls missed the first-token budget, using the values currently in `config.py`. One line per call is saved to `probes/results/nova_app_<time>.jsonl` (git-ignored), so you can look at individual calls later.

**Cost.** Keep N small: 20 calls of each kind (the default in this mode) is enough for a first p50 and a rough p95. Each call sends about 1,000 to 1,500 input tokens and gets back up to about 150 output tokens, so a run of 20 plus 20 uses roughly 60,000 input tokens and 4,000 output tokens. At Nova Pro's published list price that is a few US cents per run (the exact Sydney price is unverified, so check the Bedrock pricing page). Run `-n 3` first to check that credentials and access work, and only go up to 50 if the p95 needs more samples (for rule N4, "more than 2 of 50 without a valid tool call", you need 50 listener calls).

**On your laptop (WSL), in the `mockboard` folder.** This uses the named AWS profile you created with `aws configure --profile mockboard` (leave `--profile` out to use your default profile):

```bash
python -m probes.selftest
python -m probes.nova_probe --app-prompts -n 3 --profile mockboard --origin laptop-wsl --no-results
python -m probes.nova_probe --app-prompts -n 20 --profile mockboard --origin laptop-wsl --region ap-southeast-2
```

**On the EC2 instance in Mumbai (`ap-south-1`), in `/opt/mockboard`.** The instance role supplies the credentials, so there is no `--profile`. The instance sits in Mumbai, but `--region ap-southeast-2` makes it call Nova in Sydney, which is the path a live interview takes:

```bash
cd /opt/mockboard
python -m probes.selftest
python -m probes.nova_probe --app-prompts -n 3 --origin ec2-mumbai --region ap-southeast-2 --no-results
python -m probes.nova_probe --app-prompts -n 20 --origin ec2-mumbai --region ap-southeast-2
```

Running the same command from both machines separates network time from model time (CTO_NOTES_03 §4, rule N1 step 3). The decision rules N1 to N5 in CTO_NOTES_03 §4 read the actor's first-sentence p95, the listener's total p95, the throttle count, the listener validity count and the cache-read tokens straight from this table. While the fact cards are still the six synthetic ones, the cached prefix is about 930 tokens, which is below Nova's 1,000-token minimum for a cache checkpoint, so zero cache reads are expected; the probe prints a note saying so.

Useful options in this mode are `--skip-tool` (actor calls only), `--skip-stream` (listener calls only), `--gap-s 0.5` (wait longer between calls if you see throttles), `--model-id` (for example to try another model or an inference profile, which is the owner's decision under rule N6) and `--no-results`.

### Mode 2: synthetic prompts (the original probe)

Without `--app-prompts`, the probe makes N calls (50 by default) through ConverseStream and measures the time to first token and the total latency, and then makes N forced-tool calls built by the app's request builder in `src/mockboard/brain/llm.py` against a small toy schema. Both phases put a synthetic system prompt of about 1,500 tokens behind a cache checkpoint, which is long enough to show whether prompt caching works at all (and whether it still works with a tool attached). It does not use the app's real prompts, so use mode 1 for the numbers that set `mask_after_ms` and `MOVE_DEADLINE_MS`.

On your laptop (WSL), in the `mockboard` folder:

```bash
python -m probes.nova_probe -n 3 --profile mockboard --origin laptop-wsl
```

On the EC2 instance in Mumbai, in `/opt/mockboard`:

```bash
python -m probes.nova_probe -n 20 --origin ec2-mumbai --region ap-southeast-2
```

Useful options are `--no-cache`, `--skip-stream`, `--skip-tool`, `--gap-s 0.5` and `--no-results`.

### Both modes

Throttling errors are retried with exponential backoff and random jitter, and every throttle is counted in the table together with how long the throttled attempt took to fail (this tells us whether the `ACTOR_FAST_THROTTLE_RETRY` setting is worth turning on). Errors that retrying cannot fix (missing credentials, an unknown profile, access denied, an invalid request) stop the run straight away instead of repeating N times.

## Reading the results

Each probe adds a section to `probes/results/RESULTS.md` headed with the probe name and the date and time, followed by the parameters used, a results table and short notes (for Prisma, the raw transcript text). Remember that latency depends heavily on where you run from: numbers taken on a home connection in India will differ from numbers taken on the EC2 machine in Mumbai. The Nova probe writes the origin machine (`--origin`, or the host name if you leave it out) and the region into its heading; for the Gnani probes, write down which machine you used in the notes.
