# Auto AI Email Checker

Real-time Gmail / Outlook inbox classifier. New mail is ingested via provider webhooks, labeled with OpenAI, and pushed to a React dashboard over SSE.

**Branch:** `feature/v1`  
**Stack:** React (Vite) frontend · FastAPI backend · SQLite · OpenAI

Labels: Application Confirmation ? Application Action Required ? Screening ? Assessment ? Interview Invitation ? Interview Scheduled ? Interview Follow-up ? Offer ? Rejected / Closed ? Recruitment Alert ? Other

---

## Easy run (Windows)

One-time setup (creates `.env`, Python venv, npm install):

```bat
scripts\setup.bat
```

Start backend, frontend, and ngrok together (three console windows, if ngrok is installed):

```bat
run.bat
```

Stop the servers and tunnel started by the launcher:

```bat
stop.bat
```

Or from PowerShell:

```powershell
.\run.ps1
```

- Backend: http://127.0.0.1:8000  
- Frontend: http://127.0.0.1:5173  

Connected accounts without an active webhook are checked automatically every 2 minutes while the backend is running. Accounts with a registered, unexpired webhook use notifications instead of periodic polling; polling resumes if the subscription expires. This works without ngrok. Set `AUTO_SYNC_INTERVAL_SECONDS` and `AUTO_SYNC_MAX_MESSAGES` in `.env` to change the interval and recent-message limit. ngrok remains optional for immediate provider webhook updates.
The first **Sync** for an account imports its history and saves provider cursors. Later **Sync** runs fetch changes from those cursors. Use **Full rescan** in the account controls to scan the mailbox again; an expired cursor also triggers a full rescan automatically. Cursor state is saved only after each batch of changes is processed.
The inbox shows the current automatic sync state. If no account is connected, the sync worker stops, an account fails to sync, or the backend cannot be reached, a full-screen alert explains the issue and offers a connection or retry action. The alert clears after sync is available again.
Relative SQLite database paths are resolved from `backend/`, regardless of where the launcher is run.

Individual servers: `scripts\run-backend.bat` · `scripts\run-frontend.bat`

---

## Quick start

### 1. Environment

```bash
cp .env.example .env
```

Fill in at least:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Classification (Chat Completions) |
| `OPENAI_MODEL` | Model id, default `gpt-4o-mini` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Gmail OAuth |
| `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` | Outlook OAuth |
| `GMAIL_PUBSUB_TOPIC` | e.g. `projects/PROJECT/topics/gmail-push` |
| `WEBHOOK_BASE_URL` | Public HTTPS URL (ngrok) for Graph + docs |
| `TOKEN_ENCRYPTION_KEY` | Fernet key (optional; a local fallback exists) |
| `MICROSOFT_WEBHOOK_CLIENT_STATE` | Shared secret for Graph notifications |
| `FRONTEND_ORIGIN` | `http://localhost:5173` |

Generate a Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 2. Backend

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Health check: [http://localhost:8000/api/health](http://localhost:8000/api/health)

### 3. Frontend

Requires Node.js 18+.

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173). The Vite proxy forwards `/api` to the backend.

### 4. Public tunnel (required for webhooks)

```bash
ngrok http 8000
```

`run.bat` starts ngrok automatically when it is installed and on PATH. Check its public URL at `http://127.0.0.1:4040`. Set `WEBHOOK_BASE_URL` to that HTTPS URL (no trailing slash), e.g. `https://abc123.ngrok-free.app`, and restart the backend if the URL changed. The PowerShell launcher still requires starting ngrok separately.

---

## Google (Gmail) setup

1. Create a Google Cloud project.
2. Enable **Gmail API** and **Cloud Pub/Sub API**.
3. Configure OAuth consent screen; create **OAuth client ID** (Web application).
4. Authorized redirect URI: `http://localhost:8000/api/auth/google/callback`
5. Create a Pub/Sub topic, e.g. `gmail-push`.
6. Grant `gmail-api-push@system.gserviceaccount.com` **Pub/Sub Publisher** on that topic.
7. Create a **push** subscription to  
   `https://YOUR_TUNNEL/api/webhooks/gmail`
8. Put the full topic name in `GMAIL_PUBSUB_TOPIC`:  
   `projects/YOUR_PROJECT/topics/gmail-push`

On Connect Gmail, the backend calls `users.watch` and stores `historyId`. Pub/Sub push → fetch new messages → classify → SSE.

---

## Microsoft (Outlook) setup

1. Register an app in [Azure Portal](https://portal.azure.com/) → Microsoft Entra ID → App registrations.
2. Add redirect URI: `http://localhost:8000/api/auth/microsoft/callback` (Web).
3. Create a client secret; copy Application (client) ID and secret into `.env`.
4. API permissions (delegated): `Mail.ReadWrite`, `User.Read`, plus OpenID (`openid`, `profile`, `email`, `offline_access`).
5. Grant admin consent if your tenant requires it.
6. Set `MICROSOFT_WEBHOOK_CLIENT_STATE` to a long random string.
7. Ensure `WEBHOOK_BASE_URL` is HTTPS (Graph requirement).

On Connect Outlook, the backend creates a Graph subscription on `me/mailFolders('Inbox')/messages`. Notifications hit `/api/webhooks/outlook` (validation handshake included). Subscriptions auto-renew hourly when close to expiry.

---

## API overview

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness |
| GET | `/api/auth/google/start` | Start Gmail OAuth |
| GET | `/api/auth/google/callback` | OAuth callback |
| GET | `/api/auth/microsoft/start` | Start Outlook OAuth |
| GET | `/api/auth/microsoft/callback` | OAuth callback |
| GET | `/api/mailboxes` | Connected accounts |
| DELETE | `/api/mailboxes/{id}` | Disconnect |
| POST | `/api/mailboxes/{id}/sync` | Initial full import, then incremental sync |
| POST | `/api/mailboxes/{id}/sync/full` | Full mailbox rescan |
| POST | `/api/mailboxes/{id}/reclassify` | Re-run OpenAI labels on stored mail |
| GET | `/api/emails?label=` | Classified inbox |
| GET | `/api/events` | SSE stream (`email.classified`) |
| POST | `/api/webhooks/gmail` | Pub/Sub push |
| POST | `/api/webhooks/outlook` | Graph notifications |

---

## Repo layout

```
backend/app/          FastAPI app, OAuth, Gmail/Outlook, classifier, webhooks, SSE
frontend/src/         React dashboard (connect + live inbox)
.env.example          Environment template
```

## How email classification works

New mail and **Sync** use the active OpenAI classification prompt, with local rules for clear cases. The classifier chooses one of the eleven labels above. If no OpenAI key is configured, local rules classify clear cases and use **Other** for the rest.

**Interview Scheduled** also has a separate `interview_subtype` field: `confirmation`, `calendar_invite`, `reminder`, `reschedule`, `time_change`, or `cancellation`. These are metadata values, not extra categories. The message reader displays the subtype.

Existing stored labels are migrated when the backend starts. Old interview messages with evidence of a confirmed booking become **Interview Scheduled**; other old interview messages become **Interview Invitation**. The active classification prompt is updated to the eleven-label taxonomy. Use **Reclassify** if you want OpenAI to revisit stored messages under the new definitions; this makes API calls and can take time.

Human label corrections remain pinned during reclassification. Changing a message to **Interview Scheduled** also derives its subtype from the message text.

## Notes

- Gmail and Outlook webhooks need a public HTTPS tunnel for immediate updates. Local polling works without a tunnel.
- The backend renews existing Gmail watches and Outlook subscriptions while it is running.
