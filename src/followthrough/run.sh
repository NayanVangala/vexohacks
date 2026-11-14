#!/bin/bash
# Start Follow-Through on port 8002. Loads .env if present.
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec python3 -m uvicorn followthrough.server:app --host 0.0.0.0 --port "${PORT:-8002}"
