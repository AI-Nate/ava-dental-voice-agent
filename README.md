# Ava · AI receptionist for dental offices

Dental offices lose revenue every time a patient skips a cleaning. Most insurance plans cover three a year, and most patients use one or two. Ava is a voice agent that calls patients, answers their questions from the patient record, offers only real open times, and books the visit.

Built in one day for the Startup in One Day hackathon, on **Higgs Realtime by Boson AI**.

## What it does

- **Real phone calls.** Ava dials the patient over Twilio. Twilio Media Streams are bridged to Higgs Realtime in G.711 μ-law end to end, with no resampling.
- **Browser calls.** The same agent over your mic. The backend mints a short-lived Higgs client secret, so the API key never reaches the browser.
- **Identity first.** Ava introduces the office, asks who she is speaking with, and calls `verify_identity` before any appointment tools unlock.
- **Tools.** `get_available_slots`, `book_appointment`, `reschedule_appointment`, `cancel_appointment`, `add_to_waitlist`, `save_patient_note`, `end_call`. Every tool runs on the backend and is logged live in the UI.
- **Confirmation email** with an `.ics` calendar invite after every booking (Mailgun).
- **Office app.** Dashboard (calls, talk time, revenue, unmet demand and capacity analysis), Scheduler (week/month, open slots, waitlist, cancel a doctor's day), CRM with per-patient memory, and Settings (voice picker and agent files).
- **Campaigns.** Recall (patients due a covered cleaning), gap fill (open slots go to waitlisted patients first), and doctor-cancel (cancel a block and queue reschedule calls).
- **Agent defined in plain files.** `IDENTITY.md`, `SKILLS.md` and `TONE.md` are editable in Settings; edit the text and Ava changes.

## Layout

```
backend/
  main.py          FastAPI app: API under /voice-agent, web app at /
  routes.py        HTTP + WebSocket routes (Firebase ID token + email allowlist)
  app/             scheduling, calls + tools, campaigns, dashboard, phone bridge, email, seed data
  tests/
frontend/          static single-page app (no build step)
```

## Run it

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env               # fill in BOSON_API_KEY, FIREBASE_PROJECT_ID, VOICE_AGENT_ALLOWED_EMAILS
cp ../frontend/config.example.js ../frontend/config.js   # your Firebase web config + allowed emails
uvicorn main:app --port 8000
```

Open http://localhost:8000, sign in with Google, and the demo office is seeded on first start (about 40 fake patients with a year of history). **Settings → Reset demo data** restores it.

Real phone calls also need Twilio credentials, a public `wss://` URL for this server (`VOICE_AGENT_PUBLIC_WS_BASE`), and your own phone in `VOICE_AGENT_PHONE_ALLOW` and `VOICE_AGENT_DEMO_PHONE`. The dial guard refuses every number not on that list.

Tests: `cd backend && pytest`.

## Demo

1. Dashboard → **Queue recall calls**.
2. CRM → the demo patient → **📞 Phone call** (or **🎙 Browser call**). Ask about insurance, ask for a time, ask to be waitlisted.
3. Scheduler → the new booking and the waitlist.
4. Scheduler → **Cancel doctor's day** → reschedule calls are queued.

## License

MIT
