#!/usr/bin/env bash
# book-distiller-bot entry point. See README.md.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
if [ ! -f .venv/.requirements-installed ] || [ requirements.txt -nt .venv/.requirements-installed ]; then
  echo "Installing requirements…"
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.requirements-installed
fi
export PYTHONWARNINGS="ignore::Warning:urllib3"
exec .venv/bin/python -m distiller "$@"
