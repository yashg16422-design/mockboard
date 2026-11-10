#!/usr/bin/env bash
# Mock Board in one command: sets up and opens the Claude room (Claude as the brain, Gnani speech).
#
#   bash scripts/quickstart.sh           Gnani speech: asks once for YOUR Gnani API key
#   bash scripts/quickstart.sh --fake    no Gnani: a tone for each line, you type answers (free)
#   PORT=7871 bash scripts/quickstart.sh another port (default 7870)
#
# Needs: macOS or Linux, Python 3.12 or newer, Claude Code installed and signed in (Claude runs on
# your own Claude plan), Chrome. Run it from anywhere; it works in the repository's folder.
set -euo pipefail
cd "$(dirname "$0")/.."

FAKE=0
for arg in "$@"; do
  case "$arg" in
    --fake) FAKE=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (use --fake or --help)"; exit 2 ;;
  esac
done
PORT="${PORT:-7870}"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

say "1/4  Python 3.12 or newer"
PY=""
for candidate in python3.14 python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Mock Board needs Python 3.12 or newer. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi
echo "Using $("$PY" --version)"

say "2/4  Mock Board's packages (in .venv, this folder only)"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e '.[claude]'
echo "Installed."

say "3/4  Claude Code (the brain: your own Claude login, no API key)"
if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code is not installed. Install it (https://docs.claude.com/en/docs/claude-code),"
  echo "run 'claude' once and sign in, then run this script again."
  exit 1
fi
echo "Found Claude Code $(claude --version 2>/dev/null | head -1). If it is not signed in yet, run 'claude' once and sign in."

say "4/4  Gnani speech (Prisma hears you, Timbre speaks)"
if [ "$FAKE" = 1 ]; then
  echo "Skipped: --fake (no Gnani; you type your answers)."
else
  [ -f .env ] || cp .env.example .env
  if grep -qE '^GNANI_API_KEY=(replace-me.*)?$' .env || ! grep -q '^GNANI_API_KEY=' .env; then
    printf 'Paste your Gnani API key (it is saved only in .env here, never shown or committed): '
    read -rs KEY; echo
    if [ -z "$KEY" ]; then echo "No key given. Run again, or use --fake."; exit 1; fi
    tmp="$(mktemp)"
    grep -v '^GNANI_API_KEY=' .env > "$tmp" || true
    { echo "GNANI_API_KEY=$KEY"; cat "$tmp"; } > .env
    rm -f "$tmp"; unset KEY
    chmod 600 .env
    echo "Saved."
  else
    echo "Your Gnani key is already in .env."
  fi
fi

say "Starting Mock Board: open http://localhost:$PORT in Chrome (headphones on). Ctrl+C stops it."
if [ "$FAKE" = 1 ]; then
  exec .venv/bin/python -m mockboard.claude_room --port "$PORT" --fake-speech
fi
exec .venv/bin/python -m mockboard.claude_room --port "$PORT"
