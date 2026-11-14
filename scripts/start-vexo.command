#!/bin/sh
# Double-click in Finder to start Vexo. Safe to run again: it replaces a running copy.
cd "$(dirname "$0")/../src/backend" || exit 1

# Free the port from a previous run (Terminal closed without Ctrl+C, etc.).
OLD=$(lsof -tiTCP:8001 -sTCP:LISTEN)
[ -n "$OLD" ] && echo "Stopping old receiver ($OLD)…" && kill $OLD && sleep 1

# First run on this Mac: create the venv and install deps.
if [ ! -x .venv/bin/python ]; then
  echo "First run: installing…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
[ -f .env ] || { cp .env.example .env; echo "Created src/backend/.env: add your API keys, then run this again."; exit 1; }

IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)
echo
echo "  Vexo is starting. Open the Vexo app on the iPhone: it finds this Mac by itself."
echo "  If it doesn't, type this in the app's Glasses tab:  http://$IP:8001"
echo
exec ./run.sh
