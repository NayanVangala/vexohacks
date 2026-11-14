#!/bin/sh
# Start the receiver with keys from .env. Usage: ./run.sh
# Also advertises it over Bonjour (_vexo._tcp) so the iPhone app finds this Mac by itself.
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
PORT="${PORT:-8001}"

dns-sd -R "Vexo receiver" _vexo._tcp local "$PORT" >/dev/null 2>&1 &
BONJOUR=$!
trap 'kill $BONJOUR 2>/dev/null' EXIT INT TERM

.venv/bin/python server.py
