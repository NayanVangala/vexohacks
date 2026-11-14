# Follow-Through (agent and dashboard)

Reads a conversation transcript, finds the promises in it, proves each one with an exact quote, drafts the follow-up
(email, reply, calendar event or to-do), and does nothing until you approve each item.

## How it works

1. A transcript (pasted, uploaded audio, or recorded) is stored as numbered segments with start and end times.
2. `followthrough/llm.py` asks an OpenAI model for commitments using a strict JSON schema.
3. `followthrough/verify.py` checks each quote against the transcript in plain code. The model never marks its own work as verified. A quote that is not found, or is under 3 words, puts the item in "needs review" and it cannot be approved.
4. You approve an item in the dashboard. Only then does `followthrough/executor.py` act.
5. Status is honest. "Done" means the executor confirmed it. With no executor set up, approving only makes a draft link and says "Draft only, nothing was sent".

One function is the whole agent: `followthrough.agent.run(segments)` returns a list of items.

## Setup

```
cd followthrough-app
pip install -r requirements.txt
cp .env.example .env        # then put your key in OPENAI_API_KEY
./run.sh                    # http://localhost:8002
```

The built dashboard (`dashboard/dist`) is served by the same process, so there is nothing else to start.
To change the dashboard: `cd dashboard && npm install && npm run build` (or `npm run dev` for hot reload, proxied to port 8002).

Without a key, only the staged demo works ("Load demo"). Everything from the demo is labelled "Demo data".

## Settings (all environment variables, see .env.example)

| Name | Default | Meaning |
|---|---|---|
| OPENAI_API_KEY | none | The only value you must add |
| OPENAI_MODEL | gpt-4o | Chat model. Must support structured output. Change it here, not in code |
| OPENAI_TRANSCRIBE_MODEL | whisper-1 | Must return segment timestamps |
| WEARER_NAME, TIMEZONE | the wearer, America/Los_Angeles | Used in the prompt and to resolve "tomorrow at 3" |
| EXECUTOR_URL, EXECUTOR_TOKEN | none | Google Apps Script web app that really sends and schedules |
| DEMO_EMAIL_TO | ultrasrijucat970@gmail.com | Every email goes here, whatever the model says |
| CALENDAR_ACCOUNT | srijansaha.work@gmail.com | Shown in the UI and used for draft links |

## Real email and calendar (the executor)

`followthrough/apps_script/Code.gs` is a Google Apps Script web app. It runs as the account that deploys it.

1. Sign in as srijansaha.work@gmail.com and open script.google.com, then New project. Paste in `Code.gs`.
2. Project settings, Script properties: add `TOKEN` (any long random string) and `DEMO_TO` (the only address mail may go to).
3. Run the `authorize` function once and accept the Gmail and Calendar permissions.
4. Deploy, New deployment, type Web app, execute as Me, access Anyone. Copy the web app URL.
5. Put the URL in `EXECUTOR_URL` and the same token in `EXECUTOR_TOKEN`, then restart.

The header then reads "Sends real email and calendar". Events go on the default calendar of the account that deployed the script.

## API (used by the dashboard)

| Route | Purpose |
|---|---|
| GET /api/state | transcript, items, to-do list, settings in one call |
| POST /api/transcript | add text (`Speaker: line` per line) or segments with times |
| POST /api/transcribe | upload audio, returns timestamped segments added to the transcript |
| POST /api/run | find commitments in the transcript so far. Safe to repeat, it never duplicates or undoes an approval |
| PATCH /api/items/{id} | edit the draft. Editing never makes a made-up quote verified |
| POST /api/items/{id}/approve | the only route that acts. Pending items only |
| POST /api/items/{id}/dismiss, /retry | reject, or put a failed item back to pending |
| POST /api/demo/load, /api/reset | staged conversation, clear everything |

## Put it on a phone

The dashboard is a web page sized for phones. On the phone, open `http://<computer ip>:8002` in Safari, tap Share, then Add to Home Screen. Both devices must be on the same network (use a phone hotspot if the venue Wi-Fi isolates devices). Recording needs https or localhost, so on the phone use Upload audio or Paste text.

## Tests

`python3 -m pytest followthrough/tests` runs 29 tests with a fake model and mocked OpenAI and executor calls. No key, no network.

## What is verified and what is not

Verified (tests, plus a browser run of the staged demo):
- Quote check: exact, case and punctuation tolerant, split across lines, rejects paraphrases and quotes under 3 words, tightest match.
- Staged conversation gives 3 verified items and 1 rejected made-up quote.
- Fixed email recipient, calendar item without a time goes to review, ids are stable so re-running does not duplicate.
- Approve rules: needs-review and already-handled items are refused, double approve is refused, failed executor shows "failed" not "done", no executor shows "drafted" not "sent".
- State survives a restart. The OpenAI request is built with the strict schema and the key and model come from the environment (checked against a mocked client).
- Dashboard builds, runs in Chrome at desktop and phone sizes, shows the no-key error clearly.

Not verified (needs your key, your Google account, or real audio):
- A real OpenAI call: output quality of the prompt on real conversations, whether the default model name `gpt-4o` is the best choice, and real Whisper segments.
- The Apps Script executor: the code was written but never run, so a real email or calendar event has not been created.
- Recording and audio upload in a browser, and the page on an actual iPhone.
- Pulling audio from Nayan's glasses receiver. Nothing here talks to it yet. Today audio comes from the dashboard (record, upload) or pasted text.
