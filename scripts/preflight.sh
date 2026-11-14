#!/bin/bash
# Scene preflight: checks keys, services, tunnel and integrations before a glasses test. Prints PASS/WARN/FAIL, never a secret.
# Usage: scripts/preflight.sh            (run from anywhere; reads src/backend/.env)
cd "$(dirname "$0")/.." || exit 1
ENVF=src/backend/.env
[ -f "$ENVF" ] || { echo "FAIL  $ENVF missing: copy src/backend/.env.example and fill it in"; exit 1; }
set -a; . "$ENVF"; set +a
BASE="${SCENE_URL:-http://localhost:8001}"
fails=0; warns=0
ok()   { echo "PASS  $1"; }
warn() { echo "WARN  $1"; warns=$((warns+1)); }
bad()  { echo "FAIL  $1"; fails=$((fails+1)); }
code() { curl -s -o /dev/null -m 12 -w "%{http_code}" "$@" 2>/dev/null; }
need() { [ -n "${!1}" ] && ok "$1 set" || bad "$1 is empty ($2)"; }
opt()  { [ -n "${!1}" ] && ok "$1 set" || warn "$1 is empty ($2)"; }

echo "== keys and settings"
need GEMINI_API_KEY "seeing, notes, agent"
opt OPENAI_API_KEY "speech-to-text falls back to Gemini"
opt ELEVENLABS_API_KEY "voice falls back to the macOS voice"
need STRIPE_SECRET_KEY "orders and money requests"
need PUBLIC_BASE_URL "the https tunnel URL, no trailing slash"
need EXECUTOR_URL "real email and calendar"; need EXECUTOR_TOKEN "real email and calendar"; need DEMO_EMAIL_TO "where email lands"
opt SCHEDULING_LINK "find a time to meet"
opt SLACK_BOT_TOKEN "Slack"; opt SLACK_CHANNELS "channels Scene may post in"
case "${PUBLIC_BASE_URL%/}" in https://*) ok "PUBLIC_BASE_URL is https";; "") ;; *) bad "PUBLIC_BASE_URL must be an https tunnel URL";; esac
[ "${PUBLIC_BASE_URL: -1}" = "/" ] && bad "PUBLIC_BASE_URL has a trailing slash"
[ -n "$FOLLOWTHROUGH_FORWARD_URL" ] && ok "FOLLOWTHROUGH_FORWARD_URL set" || warn "FOLLOWTHROUGH_FORWARD_URL empty: spoken promises will not reach Follow-ups"
[ "${SCENE_SPEAK_ON:-glasses}" = "glasses" ] && warn "SCENE_SPEAK_ON=glasses: the laptop stays silent; use 'both' while testing"
[ "${SCENE_NEED_FRAMES:-3}" -lt 6 ] 2>/dev/null && warn "SCENE_NEED_FRAMES=${SCENE_NEED_FRAMES:-3}: with 0.5 s frames, notes may be offered too early (try 6)"
[ "$SCENE_DEV" = "1" ] && warn "SCENE_DEV=1: dev routes are on (fine for testing, remove for the real demo)"
[ -n "$SCENE_LOCATION" ] && ok "SCENE_LOCATION fallback set" || warn "SCENE_LOCATION empty: the ordering agent needs GPS from the phone/glasses or will ask 'where are you?'"
C="${SCENE_CONTACTS:-src/backend/data/contacts.json}"
[ -f "$C" ] && ok "contacts file found ($C)" || warn "no contacts file at $C: copy src/backend/contacts.example.json there (emails go to the demo inbox without it)"

echo "== keys actually work"
[ -n "$GEMINI_API_KEY" ] && { [ "$(code "https://generativelanguage.googleapis.com/v1beta/models" -H "x-goog-api-key: $GEMINI_API_KEY")" = 200 ] && ok "Gemini key accepted" || bad "Gemini key rejected or unreachable"; }
case "$STRIPE_SECRET_KEY" in sk_test_*|rk_test_*) [ "$(code https://api.stripe.com/v1/balance -u "$STRIPE_SECRET_KEY:")" = 200 ] && ok "Stripe test key accepted" || bad "Stripe key rejected";; "") ;; *) bad "STRIPE_SECRET_KEY is not a test-mode key (sk_test_...)";; esac
[ -n "$OPENAI_API_KEY" ] && { [ "$(code https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY")" = 200 ] && ok "OpenAI key accepted" || bad "OpenAI key rejected"; }
if [ -n "$SLACK_BOT_TOKEN" ]; then
  r=$(curl -s -m 12 -X POST https://slack.com/api/auth.test -H "Authorization: Bearer $SLACK_BOT_TOKEN")
  case "$r" in *'"ok":true'*) ok "Slack token accepted";; *) bad "Slack token rejected: $(echo "$r" | sed -E 's/.*"error":"([^"]*)".*/\1/')";; esac
fi

echo "== services"
[ "$(code $BASE/stats)" = 200 ] && ok "Scene backend is up ($BASE)" || bad "Scene backend is not answering at $BASE: cd src/backend && ./run.sh"
[ "$(code $BASE/glasses/)" = 200 ] && ok "glasses web app is served" || bad "glasses web app not served"
for p in inbox notes activity orders session contacts agent; do [ "$(code $BASE/$p)" = 200 ] && ok "GET /$p" || bad "GET /$p failed (old backend? git pull and restart)"; done
[ "$(code localhost:8002/api/state)" = 200 ] && ok "Follow-Through is up (:8002)" || warn "Follow-Through is not answering on :8002: the Follow-ups screen will be empty"
[ "$(code $BASE/ft/api/state)" = 200 ] && ok "Scene reaches Follow-Through through /ft" || warn "/ft proxy to Follow-Through failed"
if [ -n "$PUBLIC_BASE_URL" ]; then
  [ "$(code "${PUBLIC_BASE_URL%/}/glasses/")" = 200 ] && ok "tunnel reaches the glasses app (${PUBLIC_BASE_URL%/})" || bad "tunnel does not reach the backend: is cloudflared running, and is PUBLIC_BASE_URL the current URL?"
fi
n=$(curl -s -m 5 $BASE/stats | sed -E 's/.*"frames":([0-9]+).*/\1/'); case "$n" in ''|*[!0-9]*) ;; 0) warn "no camera frames yet (phone app streaming? check the Glasses tab)";; *) ok "camera frames arriving ($n so far)";; esac

echo; echo "$fails fail, $warns warn"; [ "$fails" -eq 0 ]
