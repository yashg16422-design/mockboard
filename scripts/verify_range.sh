#!/usr/bin/env bash
# Checks every commit after BASE on its own export, not trusting the hook (Ruling 60).
# Usage: scripts/verify_range.sh BASE   (from `make verify-range BASE=<sha>`)
# Prints a table for docs/ORCHESTRATOR_LOG.md. Exit 0 all green, 1 any red, 2 bad usage.
set -uo pipefail  # no -e: a red commit is recorded and the run carries on

base=${1:-}
if [ -z "$base" ]; then
    echo "usage: make verify-range BASE=<sha>" >&2
    exit 2
fi
repo=$(git rev-parse --show-toplevel) || exit 2
python=${MOCKBOARD_HOOK_PYTHON:-$repo/.venv/bin/python}
if [ ! -x "$python" ]; then
    echo "verify-range: no Python at $python. Run: make venv" >&2
    exit 2
fi
if ! commits=$(git rev-list --reverse "$base..HEAD"); then
    echo "verify-range: cannot read commits after $base" >&2
    exit 2
fi
if [ -z "$commits" ]; then
    echo "verify-range: no commits after $base" >&2
    exit 2
fi

export PYTHONPATH=src:.
red=0
echo "| commit | pytest | ruff |"
echo "|---|---|---|"
for sha in $commits; do
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    if ! git -C "$repo" archive "$sha" | tar -x -C "$tmp"; then
        echo "| ${sha:0:7} | export failed | export failed |"
        red=1
        rm -rf "$tmp"
        continue
    fi
    (cd "$tmp" && env -u GIT_DIR -u GIT_INDEX_FILE -u GIT_WORK_TREE \
        "$python" -m pytest -q >"$tmp/.pytest.log" 2>&1)
    pytest_code=$?
    summary=$(tail -n 1 "$tmp/.pytest.log")
    (cd "$tmp" && "$python" -m ruff check src tests eval >/dev/null 2>&1)
    ruff_code=$?
    echo "| ${sha:0:7} | $summary, exit $pytest_code | exit $ruff_code |"
    if [ "$pytest_code" -ne 0 ] || [ "$ruff_code" -ne 0 ]; then
        red=1
    fi
    rm -rf "$tmp"
done
exit "$red"
