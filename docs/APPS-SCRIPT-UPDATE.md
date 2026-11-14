# Let Scene email real contacts (Apps Script update, ~3 minutes)

Scene can now email people in `contacts.json` (see `src/backend/contacts.example.json`). The Apps Script is the hard
lock: until you do this, every email still goes to `DEMO_TO` (your own inbox) and the card/result will say
"(not maya@...: not on the allowlist)", so nothing is ever sent to a wrong person silently.

1. Open the Apps Script project that runs the executor (script.google.com, signed in as the account that deployed it).
2. Replace the code with `src/followthrough/followthrough/apps_script/Code.gs` from this repo (only the `email` block changed).
3. Project Settings (gear) > Script properties > add `ALLOWED_TO` = the addresses Scene may email, comma-separated,
   for example `maya@gmail.com,nayan@gmail.com`. Keep `TOKEN` and `DEMO_TO` as they are.
4. Deploy > Manage deployments > pencil on your existing web-app deployment > Version: **New version** > Deploy.
   Editing the existing deployment keeps the same URL, so `EXECUTOR_URL` does not change. (A brand new deployment
   gives a new URL: then update `EXECUTOR_URL` in `.env`.)
5. Put the same people in `contacts.json` (or `POST /contacts` with `name` and `email`).

Check it without sending anything: ask Scene "email Maya that I'll be late" and read the card. It should say
`To: maya@...`. Tap Send only when you want the real email to go out.

Safety: the model only ever supplies a name; the address comes from `contacts.json`. The email still needs your tap,
and the Apps Script refuses any address not in `ALLOWED_TO`.
