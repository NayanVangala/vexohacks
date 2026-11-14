#!/bin/sh
# Start the receiver with keys from .env. Usage: ./run.sh
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec .venv/bin/python server.py
