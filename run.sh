#!/usr/bin/env bash
# book-distiller-bot entry point. See README.md.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv…"
  py=$(command -v python3.14 || ls ~/.local/bin/python3.14 2>/dev/null || true)
  [ -n "$py" ] || { echo "Needs Python 3.14: ~/.local/bin/uv python install 3.14" >&2; exit 1; }
  "$py" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
.venv/bin/python -c "import sys; sys.exit(sys.version_info < (3, 14))" ||
  { echo "The .venv is older than Python 3.14: delete .venv and run again" >&2; exit 1; }
if [ ! -f .venv/.requirements-installed ] || [ requirements.txt -nt .venv/.requirements-installed ]; then
  echo "Installing requirements…"
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.requirements-installed
fi
export PYTHONWARNINGS="ignore::Warning:urllib3"
exec .venv/bin/python -m distiller "$@"
