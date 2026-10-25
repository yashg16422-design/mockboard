# Mock Board developer targets. Run from the repository root (Laptop (WSL), `~/mockboard`).
# Probe targets are described by Builder 2 in probes/README.md.
PYTHON ?= python3
export PYTHONPATH := src:.

.PHONY: help install test lint format cli cli-fake cli-fake-hi eval clean-traces

help:
	@echo "install      install runtime + dev dependencies (pinned)"
	@echo "test         run the offline unit tests (FakeLLM only)"
	@echo "lint         ruff check"
	@echo "cli          interactive text interview, fake offline LLM"
	@echo "cli-fake     scripted fake interview (examples/fake_session.txt)"
	@echo "cli-real     interactive text interview on Nova Pro (costs money; needs AWS creds)"
	@echo "eval         run the example eval set with the fake LLM (dev split)"

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

clean-traces:
	rm -rf traces
