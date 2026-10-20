# Auto AI Email Checker

Native Windows Gmail and Outlook email classifier with interview calendar tracking.

## Architecture

```text
Gmail / Outlook
  -> OAuth + webhook
  -> sync-worker
  -> durable webhook job
  -> import and deduplicate in PostgreSQL
  -> transactional outbox
  -> event-relay
  -> RabbitMQ email.imported.v1 queue
  -> classification-worker
  -> AI classification and extraction
  -> calendar event/chip update
  -> RabbitMQ user.activity.v1 routing key
  -> notification-service
  -> durable in-app notification
  -> RabbitMQ notification.ready.v1 routing key
  -> authenticated WebSocket
  -> React UI
```

PostgreSQL is the durable source of truth. RabbitMQ provides acknowledged work
delivery and live-event fan-out. The notification service is an independent
process that consumes user activity, persists in-app notifications, and emits
delivery-ready events for the API WebSocket gateway. The API, sync worker,
classification worker, event relay, notification service, PostgreSQL,
RabbitMQ, Vite, and optional ngrok tunnel all run directly on Windows.

## Requirements

- Windows PowerShell
- The project-local PostgreSQL installation under `.local/postgresql`
- Erlang/OTP and RabbitMQ Server installed as a Windows service
- Python virtual environment at `backend/.venv`
- Frontend dependencies under `frontend/node_modules`
- OAuth credentials for each enabled email provider
- An OpenAI or Gemini API key, or a reachable Ollama service

## First-time setup

If `.env` does not exist, copy the native template and add your credentials:

```powershell
Copy-Item .env.example .env
notepad .env
```

Install RabbitMQ from an Administrator PowerShell:

```powershell
.\scripts\install-rabbitmq.ps1
```

The installer resumes interrupted downloads, verifies official installer
checksums, installs RabbitMQ as an automatic Windows service, and enables its
management plugin.

Install application dependencies if needed:

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt
Set-Location frontend
npm install
Set-Location ..
```

## Run

Start locally:

```powershell
.\run.ps1
```

Start with the webhook tunnel and open the browser:

```powershell
.\run.bat
```

`run.bat` enables backend development reload. Changes under `backend/app`
reload FastAPI and restart the sync, classification, event-relay, and
notification worker processes. Vite continues to hot-reload frontend changes.

To run without backend reload, use `run.ps1` directly.

Open `http://localhost:5173`. Logs are written under `logs/`.

Stop all application services:

```powershell
.\stop.bat
```

The launcher verifies RabbitMQ before starting and attempts to start the
Windows `RabbitMQ` service when necessary.

## OAuth and webhooks

### Gmail

1. Enable Gmail API and Cloud Pub/Sub in Google Cloud.
2. Create a Web OAuth client.
3. Register `https://YOUR_HOST/api/auth/google/callback` as a redirect URI.
4. Create a Pub/Sub topic and grant
   `gmail-api-push@system.gserviceaccount.com` publisher access.
5. Push Pub/Sub notifications to `https://YOUR_HOST/api/webhooks/gmail`.

### Outlook

1. Register an application in Microsoft Entra ID.
2. Register `https://YOUR_HOST/api/auth/microsoft/callback` as a Web redirect URI.
3. Grant delegated `Mail.ReadWrite`, `User.Read`, `openid`, `profile`, `email`,
   and `offline_access` permissions.
4. Set a long random `MICROSOFT_WEBHOOK_CLIENT_STATE` value.

## Operational endpoints

```text
GET /api/health
GET /api/health/ready
GET /api/health/events
GET /api/activity?after_sequence=0
GET /api/notifications
GET /api/notifications?unread_only=true
POST /api/notifications/{notification_id}/read
POST /api/notifications/read-all
WS  /api/ws/activity
```

## Repository layout

```text
backend/       FastAPI API, workers, integrations, and tests
frontend/      React and Vite user interface
scripts/       Native Windows setup and process supervision
run.ps1        Native application launcher
run.bat        Launcher with ngrok enabled
stop.bat       Native service shutdown
.env.example   Native environment template
```

## Service boundaries

- `email-api` owns REST, OAuth callbacks, webhook admission, authenticated
  WebSocket delivery, and reconnect recovery.
- `sync-worker` owns Gmail and Microsoft Graph communication after requests are
  durably accepted.
- `classification-worker` owns model calls and calendar projections.
- `event-relay` delivers PostgreSQL transactional-outbox events to RabbitMQ.
- `notification-service` consumes `user.activity.v1`, persists idempotent
  in-app notifications, and emits `notification.ready.v1` delivery events.

Manual sync and reclassification requests use the durable mailbox-job queue.
Workers persist progress and honor stop requests across process boundaries.
