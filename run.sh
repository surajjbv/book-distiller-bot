#!/usr/bin/env bash
# Book Distiller entry point. See README.md.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
export PYTHONWARNINGS="ignore::Warning:urllib3"
exec .venv/bin/python -m distiller "$@"
