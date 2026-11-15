#!/bin/sh
# Double-click in Finder to start all of Scene: Cloudflare tunnel, Scene
# backend (8001), and Follow-Through (8002), in one Terminal window.
# Safe to run again: it replaces any running copy (no "address already in use").
# Stop with Ctrl+C in the Terminal window, or bash scripts/stop-scene.sh.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "$ROOT/scripts/launch.sh"
