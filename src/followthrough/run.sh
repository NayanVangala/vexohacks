#!/bin/bash
# Start Follow-Through on port 8002. Loads .env if present.
# Uses its own venv (same pattern as src/backend/run.sh): the system python3
# never had requirements.txt installed, so uvicorn crashed on startup with
# "Form data requires python-multipart" and Follow-Through never came up --
# every forward from Scene then failed with Connection refused and no items
# ever appeared. First run creates the venv and installs deps (takes a while).
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Follow-Through venv..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python -m uvicorn followthrough.server:app --host 0.0.0.0 --port "${PORT:-8002}"
