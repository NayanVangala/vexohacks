#!/bin/bash
# Scene one-command launcher.
# Starts everything in ONE terminal window: Cloudflare tunnel, Scene backend
# (port 8001), and Follow-Through (port 8002). Safe to run again: it kills any
# old copy on 8001/8002 first, so no more "address already in use".
#
# Usage:  bash scripts/launch.sh
#   or:   double-click scripts/start-scene.command in Finder (it calls this).
# Stop:   press Ctrl+C in this window, or run bash scripts/stop-scene.sh.
# Logs:   /tmp/scene-backend.log, /tmp/scene-followthrough.log, /tmp/scene-tunnel.log
# Ports, keys and .env loading are unchanged; only PUBLIC_BASE_URL in
# src/backend/.env is updated to the new tunnel URL each launch (the tunnel
# URL is random every run, and the backend + preflight expect them to match).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

BACKEND_LOG=/tmp/scene-backend.log
FT_LOG=/tmp/scene-followthrough.log
TUNNEL_LOG=/tmp/scene-tunnel.log
BACKEND_PID=""; FT_PID=""; TUNNEL_PID=""
IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || echo localhost)

cleanup() {
  echo
  echo "Stopping Scene..."
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
  [ -n "$FT_PID" ] && kill "$FT_PID" 2>/dev/null
  [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null
  # Give the backend up to 10 s to finish: an in-flight extract (5-15 s) and
  # Follow-Through forward die instantly on kill -9, and the work they were
  # doing would be lost. The durable pending rows mean a kill still recovers,
  # but a graceful exit is cleaner.
  for _ in $(seq 1 10); do
    sleep 1
    alive=0
    for p in "$TUNNEL_PID" "$FT_PID" "$BACKEND_PID"; do
      [ -n "$p" ] && kill -0 "$p" 2>/dev/null && alive=1
    done
    [ "$alive" = 0 ] && break
  done
  [ -n "$TUNNEL_PID" ] && kill -9 "$TUNNEL_PID" 2>/dev/null
  [ -n "$FT_PID" ] && kill -9 "$FT_PID" 2>/dev/null
  [ -n "$BACKEND_PID" ] && kill -9 "$BACKEND_PID" 2>/dev/null
  echo "Scene stopped. Run bash scripts/launch.sh again to restart."
}
trap cleanup EXIT INT TERM

kill_port() { # $1 = port number
  local pids
  pids=$(lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null)
  if [ -n "$pids" ]; then
    echo "Stopping whatever is already on port $1 ($pids)..."
    kill $pids 2>/dev/null
    # Wait up to 10 s for a graceful exit before kill -9: the old backend may
    # still be extracting the last transcript batch, and killing it mid-extract
    # used to lose that batch. (The durable pending rows recover it anyway.)
    for _ in $(seq 1 10); do
      sleep 1
      pids=$(lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null)
      [ -z "$pids" ] && break
    done
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
  fi
}

wait_for() { # $1 = url, $2 = label; waits up to 30s for a 200
  local i
  for i in $(seq 1 30); do
    if [ "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null)" = "200" ]; then
      echo "OK   $2"
      return 0
    fi
    sleep 1
  done
  echo "NOT UP  $2 ($1)"
  return 1
}

echo "Starting Scene..."
kill_port 8001
kill_port 8002
pkill -f "cloudflared tunnel --url http://localhost:8001" 2>/dev/null || true

# First run on this Mac: create the backend venv and install deps.
if [ ! -x src/backend/.venv/bin/python ]; then
  echo "First run: setting up the backend..."
  (cd src/backend && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt) || exit 1
fi
if [ ! -f src/backend/.env ]; then
  [ -f src/backend/.env.example ] && cp src/backend/.env.example src/backend/.env
  echo "Created src/backend/.env: add your API keys, then run this again."
  exit 1
fi

# Tunnel first: the backend reads PUBLIC_BASE_URL at startup, and the tunnel
# URL is different every run, so grab it before starting the backend.
TUNNEL_URL=""
if command -v cloudflared >/dev/null 2>&1; then
  echo "Starting Cloudflare tunnel..."
  cloudflared tunnel --url http://localhost:8001 >"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
  for i in $(seq 1 20); do
    TUNNEL_URL=$(grep -oE 'https://[A-Za-z0-9.-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
    [ -n "$TUNNEL_URL" ] && break
    sleep 1
  done
  if [ -n "$TUNNEL_URL" ]; then
    echo "Tunnel is up: $TUNNEL_URL"
    export PUBLIC_BASE_URL="$TUNNEL_URL"
    if grep -q '^PUBLIC_BASE_URL=' src/backend/.env; then
      sed -i '' "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$TUNNEL_URL|" src/backend/.env
    else
      printf '\nPUBLIC_BASE_URL=%s\n' "$TUNNEL_URL" >> src/backend/.env
    fi
  else
    echo "Tunnel did not come up in 20s, continuing with localhost only (log: $TUNNEL_LOG)."
    TUNNEL_PID=""
  fi
else
  echo "cloudflared is not installed, continuing with localhost only."
  echo "To get a tunnel (needed for the glasses display), run: brew install cloudflared"
fi

echo "Starting Follow-Through on port 8002..."
(cd src/followthrough && ./run.sh >"$FT_LOG" 2>&1) &
FT_PID=$!

echo "Starting Scene backend on port 8001..."
(cd src/backend && ./run.sh >"$BACKEND_LOG" 2>&1) &
BACKEND_PID=$!

echo "Waiting for everything to answer..."
wait_for http://localhost:8001/stats "Scene backend"
wait_for http://localhost:8001/glasses/ "glasses web app"
wait_for http://localhost:8001/ft/api/state "Follow-Through (through Scene)"

echo
echo "  Scene is up."
echo "  Backend:  http://localhost:8001  (on your network: http://$IP:8001)"
if [ -n "$TUNNEL_URL" ]; then
  echo "  Tunnel:   $TUNNEL_URL"
  echo "  Glasses:  $TUNNEL_URL/glasses/"
else
  echo "  Tunnel:   not running (localhost only, glasses display needs the tunnel)"
  echo "  Glasses:  open http://$IP:8001/glasses/ in a browser to check the app"
fi
echo "  Phone:    open the Scene app, tap Start, then Connect glasses."
echo "            (It usually finds this Mac by itself. If not, type this"
echo "             in the Glasses tab: http://$IP:8001)"
echo "  Logs:     $BACKEND_LOG  $FT_LOG  $TUNNEL_LOG"
echo "  Keep this window open. Press Ctrl+C to stop everything."
echo

# Stay in the foreground until the backend exits (or Ctrl+C); the trap above
# stops Follow-Through and the tunnel too.
wait "$BACKEND_PID" 2>/dev/null
