# Developing Mock Board

Everything needed to run, test and change Mock Board from source. The product overview is the
[README](../README.md); the learning guide is [WALKTHROUGH.md](WALKTHROUGH.md).

## Prerequisites

- macOS, Linux or WSL with Python 3.12 or newer (the full suite has been run on Python 3.14 only).
- `make` and `git`.
- For the offline interview, the tests and the eval, nothing else is needed: everything runs on your machine with a rule-based fake model and costs nothing.
- For the real model only: an AWS account with access to Amazon Nova Pro (`amazon.nova-pro-v1:0`) in the Sydney region (`ap-southeast-2`), and the AWS CLI configured with a profile. Real calls cost money.
- For the real voice app and the probes: a Gnani API key in `.env` as `GNANI_API_KEY` (real speech spends Gnani credits).

## Setup

You can either install the package once, or run it straight from the source folder.

**Option 1: install it.** On your laptop (WSL), in the `mockboard` folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
make install
```

`make install` installs the pinned dependencies from `requirements.txt` and then installs Mock Board itself in editable mode, so your code changes take effect without reinstalling.

**Option 2: run from source.** Every `make` target already sets `PYTHONPATH=src:.`, so `make` commands work without installing the package (you still need the dependencies from `requirements.txt`). If you call Python yourself instead of using `make`, you must set the path, or Python will say it cannot find the `mockboard` module. On your laptop (WSL), in the `mockboard` folder:

```bash
pip install -r requirements.txt
PYTHONPATH=src python3 -m mockboard.cli --fake
```

To change settings, copy `.env.example` to `.env` and edit it. Every setting has a safe default, so this is optional for the offline interview. The `.env` file is ignored by git, and AWS credentials never go in it.

## Run the offline interview

The offline interview uses a rule-based fake model that imitates the shape of Nova's answers. It shows how the whole pipeline behaves, but its words and judgements are not Nova's.

To watch a complete scripted interview (the answers come from `examples/fake_session.txt`), with a short trace summary at the end. On your laptop (WSL), in the `mockboard` folder:

```bash
make cli-fake
```

In this run you can see the whole panel (since P7, the UPSC interview is always the panel): the Chairperson's ritual line, five members in order with a spoken hand-off at each change, a wrong figure questioned a turn after it was said and the acknowledgement when the candidate admits it, a repeat that costs no answer, a topic change that never comes back, an honest "I don't know" reassured, and the Chairperson's close. The full transcript, read line by line, is in [docs/tasks/P7_FAKE_TRANSCRIPT.md](tasks/P7_FAKE_TRANSCRIPT.md). `--max-answers 10` caps the interview the way a test user's is capped (the old `--panel` flag still works and does nothing). `make cli-fake-hi` runs the same script and writes the report in Hindi.

To be interviewed yourself, still offline. On your laptop (WSL), in the `mockboard` folder:

```bash
make cli
```

The program first asks a few application-form questions (home state, district, degree subject, hobbies and so on; it never asks for a name or any identifier). Then type one answer per prompt. Typing ` | ` inside an answer splits it into two transcript segments, as if the speech recogniser had heard a pause there. Type `/quit` to stop early and go straight to the report.

The other two modes, offline and scripted:

```bash
make cli-fake-debate
make cli-fake-partner
```

## Run the Claude room (Claude as the brain, Gnani speech)

The same board with a stronger mind, as a local web app. Claude runs through **your own Claude
Code login** (no API key) with only Mock Board's tools; Gnani Prisma hears you and Gnani Timbre
speaks each member; Mock Board's code still decides and checks every move.

```bash
.venv/bin/python -m pip install -e ".[claude]"
```

```bash
make claude-room
```

Open **http://localhost:7870** in Chrome (headphones on), fill the form or a study topic, press
**Start with Claude**. You watch the board prepare (an animated graph of your form, the members,
their lines of questioning and the weak spots they expect), then the sitting: the room, live
captions, and how the board is deciding, including each move Claude proposes (dig, elaborate,
confront with your own words, pivot) and whether Mock Board allowed it. The report appears on the
page. `make claude-room-fake` is the same without Gnani (type your answers; free). Claude Code
must be installed and logged in on this Mac.

## Run the voice app

Free, in your browser, with the fake model and fake speech (nothing leaves your machine). In the `mockboard` folder:

```bash
make voice-install
make voice-fake
```

Then open http://localhost:7860 in Chrome, press Begin and pick a mode. The real thing (Gnani Prisma and Timbre for speech, Nova Pro for the board; costs credits) is `make voice-upsc`; the same server offers all three modes from the chooser. Use headphones so the board does not hear itself.

## Run against Amazon Nova Pro

This uses the real model and costs money, so try the offline interview first. Mock Board reads AWS credentials only through the standard AWS credential chain (your CLI profile on the laptop, the instance role on a server); no key is ever written in the code or in `.env`. The region and model id default to `ap-southeast-2` and `amazon.nova-pro-v1:0`, and can be changed with `AWS_REGION` and `NOVA_MODEL_ID`.

On your laptop (WSL), in the `mockboard` folder:

```bash
aws configure --profile mockboard     # once: your access key, secret key, region ap-southeast-2
export AWS_PROFILE=mockboard
make cli-real
```

If the Bedrock client cannot be created (for example, the profile does not exist), the program stops with a readable message and suggests `--fake`. The timeouts used for Nova are placeholders marked `UNVERIFIED` in `src/mockboard/config.py` until the Nova probe has measured real latencies.

## Run the tests and the linter

The test suite runs without real Gnani or Bedrock calls; the voice tests use local fake services and sockets, so a full run can take a few minutes. On your laptop, in the `mockboard` folder:

```bash
make test
make lint
```

Commits are gated: `make venv` (or `make hooks` on an existing checkout) switches on `.githooks/pre-commit`, which runs both checks on what is staged, not on your working folder. A full run takes a few minutes. `.githooks/pre-merge-commit` does the same for merges. A commit touching only `docs/`, `README.md` or `CLAUDE.md` skips the tests (Ruling 67). The voice extra must be installed (`make voice-install`), or the gate refuses. `--no-verify` is not used on this project. To check each commit afterwards without trusting the hook, run `make verify-range BASE=<sha>`.

## Run the evaluation

The evaluation sends scripted answers through the real analysis graphs and counts how many planted factual errors the board catches and whether it ever "corrects" a true statement. On your laptop (WSL), in the `mockboard` folder:

```bash
make eval
```

It runs the development split and writes a summary file to `eval/results/`. The held-out split refuses to run unless you pass `--allow-heldout`, because it is meant only for milestone checks. Today the eval set contains six illustrative example items that cite synthetic test cards; the real frozen golden set (60 answers and about 150 hand-checked true claims) has not been written yet. With the fake model, the numbers test the pipeline, not Nova's quality. `docs/WALKTHROUGH.md` explains each metric.

The eval gate runs the same dev split and fails (with a non-zero exit code) if planted-error recall falls below a threshold or if any true claim is "corrected". The thresholds are the settings `EVAL_GATE_MIN_PLANTED_RECALL` (default 0.8) and `EVAL_GATE_MAX_FALSE_CORRECTIONS` (default 0). On your laptop (WSL), in the `mockboard` folder:

```bash
make eval-gate
make eval-gate EVAL_BASELINE=eval/baselines/dev.json   # optional: also compare with a committed baseline summary
```

No baseline file is committed yet; the first one should be a run on the frozen golden dev set.

## The probes

The probes measure the three outside services (Prisma, Timbre and Nova) before we build on them, and record the results in `probes/results/RESULTS.md`. They spend real Gnani credits and AWS money, so always start with the offline self-test. On your laptop (WSL), in the `mockboard` folder:

```bash
python3 -m probes.selftest
```

Full instructions for every probe, including which machine to run each one from, are in [probes/README.md](../probes/README.md).

## Where traces go

Every interview writes one file, `traces/<session_id>.jsonl` (the folder can be changed with `TRACE_DIR`). The file has one JSON line when the session starts, one line per candidate turn, and one line for the report. A turn line records the move the board chose and the rule that chose it, the line it spoke, the timestamp of every stage (in milliseconds since the turn started), every model call with its lane, latency and token counts, and every fallback that fired. The `traces/` folder is ignored by git, because traces contain full transcripts.

Eval summaries go to `eval/results/`. A report is printed at the end of each interview, and `--report-json <path>` also saves it as JSON.

## Repository map

- `src/mockboard/session.py` is the session controller. It runs one interview from the opening question to the report, through the three calls the voice loop will also use: `submit_segment`, `end_of_turn` and `speak`.
- `src/mockboard/brain/` is the "slow brain". `graphs.py` holds the three LangGraph graphs (fast analysis, fact check, report). `listener.py`, `librarian.py`, `strategist.py` and `judge.py` hold the logic of each graph node. `actor.py` turns a chosen move into the spoken line and enforces the guards. `llm.py` is the only module that talks to Bedrock. `numbers.py` and `spoken.py` handle Indian number words and how figures are spoken. `cards.py`, `schemas.py`, `tools.py`, `prompts.py` and `deps.py` hold the data models and the wiring. `personas/` has one YAML file per panel member (the debate's are in `personas/debate/`). `director.py`, `agenda.py` and `agenda_plan.py` are the panel's interview director, agenda and agenda planner, and `modes/upsc_panel/` holds its session hooks. `fake_responders.py` is the offline fake model.
- `src/mockboard/prompts/` holds every prompt as a versioned file, with a changelog.
- `src/mockboard/obs/` holds the per-turn trace recorder and an optional Langfuse hook (off by default).
- `src/mockboard/cli.py` is the text-mode interview, and `src/mockboard/config.py` holds every setting.
- `data/cards/` holds the fact cards and the guide for writing real ones ([data/cards/README.md](../data/cards/README.md)).
- `eval/` holds the evaluation harness, `tests/` the offline tests, `probes/` the service probes, and `examples/` the scripted session.
- `docs/` holds the plan, the engineering standards, the CTO reviews, the orchestrator's rulings, the learning guide [docs/WALKTHROUGH.md](WALKTHROUGH.md), and the decision records in [docs/adr/](adr/).
