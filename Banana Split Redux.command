#!/bin/bash
# Double-click in Finder (or run in Terminal) to start Banana Split Redux.
# First run creates a private Python environment in .venv (needs internet once).
set -e
cd "$(dirname "$0")"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "Banana Split Redux needs Python 3.10 or newer."
  echo "Install it with Homebrew:  brew install python@3.12"
  echo "or from https://www.python.org/downloads/macos/ , then run this again."
  read -r -p "Press Return to close." _
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv with $PY …"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
fi
if ! .venv/bin/python -c "import av, numpy, PIL, PySide6" 2>/dev/null; then
  echo "Installing dependencies (PyAV with bundled FFmpeg, NumPy, Pillow, PySide6) …"
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m bsr_ui "$@"
