# Mock Board developer targets. Run from the repository root (your laptop, in the mockboard folder).
# First time on macOS (Homebrew Python) or any PEP 668 system: `make venv`, then the other targets
# use .venv automatically.
# Probe targets are described by Builder 2 in probes/README.md.
VENV_PY := .venv/bin/python
PYTHON ?= $(if $(wildcard $(VENV_PY)),$(VENV_PY),python3)
export PYTHONPATH := src:.

.PHONY: help venv hooks verify-range install test lint format cli cli-fake cli-fake-hi cli-real cli-fake-debate cli-debate cli-real-debate likely-questions check-trace eval eval-gate eval-motions eval-debate clean-traces voice-install voice-fake voice-upsc voice-debate voice-summary voice-record stt-ab voice-lab voice-lab-fake

help:
	@echo "venv         create .venv and install everything (run this first on macOS)"
	@echo "hooks        switch on the versioned pre-commit hook (repo-local git setting)"
	@echo "verify-range check each commit on its own export: make verify-range BASE=<sha>"
	@echo "install      install runtime + dev dependencies (pinned)"
	@echo "test         run the offline unit tests (FakeLLM only)"
	@echo "lint         ruff check"
	@echo "cli          interactive text interview, fake offline LLM"
	@echo "cli-fake     scripted fake interview (examples/fake_session.txt)"
	@echo "cli-real     interactive text interview on Nova Pro (costs money; needs AWS creds)"
	@echo "likely-questions the panel's likely questions for the sample form, with hints (fake, free)"
	@echo "check-trace      check a panel session trace: make check-trace TRACE=traces/<id>.jsonl"
	@echo "cli-fake-debate  scripted fake debate (examples/fake_debate.txt)"
	@echo "cli-debate       interactive text debate, fake offline LLM"
	@echo "cli-real-debate  interactive text debate on Nova Pro (costs money; needs AWS creds)"
	@echo "eval         run the example eval set with the fake LLM (dev split)"
	@echo "eval-gate    run the dev eval and fail on low recall or any false correction"
	@echo "eval-motions debate: labelled custom-motion set through the safety screen (fake)"
	@echo "eval-debate  debate: scripted debates; fails on any invariant violation or report problem"
	@echo "voice-install    add the pinned voice extra (Pipecat, Gnani plugin, aiortc) to .venv"
	@echo "voice-fake       voice app on http://localhost:7860, fake model and fake speech (free)"
	@echo "voice-upsc       voice app, UPSC board, real Prisma + Nova Pro + Timbre (costs money)"
	@echo "voice-debate     voice app, debate: make voice-debate MOTION=1 SIDE=for (costs money)"
	@echo "voice-summary    timings of the newest voice session (safe to paste: no text)"
	@echo "voice-record     voice-upsc that also saves YOUR mic audio to traces/<session>.mic.wav"
	@echo "stt-ab           replay a recording through 3 Prisma settings: make stt-ab WAV=traces/x.mic.wav"
	@echo "voice-lab        http://localhost:7861: hear board lines per Timbre model/voice/setting (credits)"
	@echo "voice-lab-fake   the same page with tones instead of Timbre (free)"

venv:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements.txt -r requirements-probes.txt
	.venv/bin/python -m pip install -e . --no-deps
	git config core.hooksPath .githooks

# Ruling 60: every commit is gated on the index (CTO_NOTES_12 section 5). Repo-local setting.
hooks:
	git config core.hooksPath .githooks

BASE ?=
verify-range:
	bash scripts/verify_range.sh $(BASE)

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -e . --no-deps

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests eval

format:
	$(PYTHON) -m ruff format src tests eval

cli:
	$(PYTHON) -m mockboard.cli --fake

cli-fake:
	$(PYTHON) -m mockboard.cli --fake --script examples/fake_session.txt --print-trace

cli-fake-hi:
	$(PYTHON) -m mockboard.cli --fake --script examples/fake_session.txt --language hi

cli-real:
	$(PYTHON) -m mockboard.cli

# P9, Utility A: likely questions from the form. Free: the offline stand-in writes the hints.
# For the real thing (one Nova Pro call, costs credits):
#   .venv/bin/python -m mockboard.cli --likely-questions --form-file <your form>.json
likely-questions:
	$(PYTHON) -m mockboard.cli --fake --likely-questions --form-file examples/forms/sample_form.json

TRACE ?=
check-trace:
	@test -n "$(TRACE)" || (echo "usage: make check-trace TRACE=traces/<session>.jsonl" && exit 2)
	$(PYTHON) -m eval.panel.check_trace $(TRACE)

# Debate mode (ruling 19, CTO_NOTES_04): the board argues the other side of a curated motion.
cli-fake-debate:
	$(PYTHON) -m mockboard.cli --mode debate --fake --script examples/fake_debate.txt --print-trace

cli-debate:
	$(PYTHON) -m mockboard.cli --mode debate --fake

cli-real-debate:
	$(PYTHON) -m mockboard.cli --mode debate

eval:
	$(PYTHON) -m eval.runner --split dev

# Thresholds: EVAL_GATE_MIN_PLANTED_RECALL, EVAL_GATE_MAX_FALSE_CORRECTIONS (settings).
# Optional: make eval-gate EVAL_BASELINE=eval/baselines/dev.json
EVAL_BASELINE ?=
eval-gate:
	$(PYTHON) -m eval.gate --split dev $(if $(EVAL_BASELINE),--baseline $(EVAL_BASELINE),)

# Debate mode: the custom-motion safety screen gate (zero partisan motions may pass).
eval-motions:
	$(PYTHON) -m eval.motions.runner

# Debate mode (DB7): scripted debates, the CTO_NOTES_04 §7 invariants and the report checks.
# Offline by default; `$(PYTHON) -m eval.debate.runner --real` runs it on Nova (costs money).
eval-debate:
	$(PYTHON) -m eval.debate.runner

# Voice app (docs/VOICE_QUICKSTART.md). Real runs need GNANI_API_KEY in .env and AWS_PROFILE.
voice-install:
	$(PYTHON) -m pip install -e '.[voice]'

voice-fake:
	$(PYTHON) -m mockboard.voice.server --mode upsc --fake-llm --fake-speech

voice-upsc:
	$(PYTHON) -m mockboard.voice.server --mode upsc

MOTION ?= 1
SIDE ?= for
voice-debate:
	$(PYTHON) -m mockboard.voice.server --mode debate --motion $(MOTION) --side $(SIDE)

voice-summary:
	$(PYTHON) -m mockboard.voice.summarize

# Ruling 35: opt-in recording of the candidate's own microphone (never the board), saved only
# under traces/, then an offline A/B of Prisma settings on it (costs Gnani credits per arm).
voice-record:
	$(PYTHON) -m mockboard.voice.server --mode upsc --record-audio

WAV ?=
stt-ab:
	$(PYTHON) -m mockboard.voice.stt_ab $(WAV)

# Ruling 38: pick the board's voice by ear. Each new render spends Timbre credits; renders are
# cached under traces/voice_lab/, so replays are free.
voice-lab:
	$(PYTHON) -m mockboard.voice.lab

voice-lab-fake:
	$(PYTHON) -m mockboard.voice.lab --fake

clean-traces:
	rm -rf traces
