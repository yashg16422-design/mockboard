# Mock Board developer targets. Run from the repository root (your laptop, in the mockboard folder).
# First time on macOS (Homebrew Python) or any PEP 668 system: `make venv`, then the other targets
# use .venv automatically.
# Probe targets are described by Builder 2 in probes/README.md.
VENV_PY := .venv/bin/python
PYTHON ?= $(if $(wildcard $(VENV_PY)),$(VENV_PY),python3)
export PYTHONPATH := src:.

.PHONY: help venv install test lint format cli cli-fake cli-fake-hi cli-real eval eval-gate clean-traces

help:
	@echo "venv         create .venv and install everything (run this first on macOS)"
	@echo "install      install runtime + dev dependencies (pinned)"
	@echo "test         run the offline unit tests (FakeLLM only)"
	@echo "lint         ruff check"
	@echo "cli          interactive text interview, fake offline LLM"
	@echo "cli-fake     scripted fake interview (examples/fake_session.txt)"
	@echo "cli-real     interactive text interview on Nova Pro (costs money; needs AWS creds)"
	@echo "eval         run the example eval set with the fake LLM (dev split)"
	@echo "eval-gate    run the dev eval and fail on low recall or any false correction"

venv:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements.txt -r requirements-probes.txt
	.venv/bin/python -m pip install -e . --no-deps

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

eval:
	$(PYTHON) -m eval.runner --split dev

# Thresholds: EVAL_GATE_MIN_PLANTED_RECALL, EVAL_GATE_MAX_FALSE_CORRECTIONS (settings).
# Optional: make eval-gate EVAL_BASELINE=eval/baselines/dev.json
EVAL_BASELINE ?=
eval-gate:
	$(PYTHON) -m eval.gate --split dev $(if $(EVAL_BASELINE),--baseline $(EVAL_BASELINE),)

clean-traces:
	rm -rf traces
