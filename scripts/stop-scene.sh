#!/bin/bash
# Stop everything scripts/launch.sh started: Scene backend (8001),
# Follow-Through (8002), and the Cloudflare tunnel to 8001.
# Usage:  bash scripts/stop-scene.sh
# (Pressing Ctrl+C in the launch window does the same thing.)
set -u

for port in 8001 8002; do
  pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null)
  if [ -n "$pids" ]; then
    echo "Stopping port $port ($pids)..."
    kill $pids 2>/dev/null
    # Wait up to 10 s for a graceful exit before kill -9: the backend may
    # still be extracting the last transcript batch, and killing it mid-extract
    # used to lose that batch. (The durable pending rows recover it anyway.)
    for _ in $(seq 1 10); do
      sleep 1
      pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null)
      [ -z "$pids" ] && break
    done
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
  else
    echo "Nothing on port $port."
  fi
done

pkill -f "cloudflared tunnel --url http://localhost:8001" 2>/dev/null && echo "Tunnel stopped." || echo "No tunnel running."
pkill -f 'dns-sd -R "Scene receiver"' 2>/dev/null || true
# If the launcher is still running in another window, stop it too so it
# does not restart anything. (This script itself does not match that pattern.)
pkill -f "scripts/launch.sh" 2>/dev/null || true

echo "Scene stopped."
