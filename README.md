# Auto AI Email Checker

Docker-only Gmail and Outlook email classifier with interview calendar tracking.

## Architecture

Docker Compose starts these services:

- `frontend`: React production build served by Nginx on port `8080`.
- `email-api`: FastAPI REST, OAuth, webhook and WebSocket service.
- `email-worker`: mailbox synchronization, reconciliation, AI processing and Kafka consumers.
- `postgres`: durable email, calendar, activity and transactional-outbox storage.
- `kafka`: asynchronous email and UI activity events.
- `schema-registry`: event-contract registry.

New mail follows this path:

```text
Gmail / Outlook
  -> OAuth + webhook
  -> durable webhook job
  -> import and deduplicate
  -> transactional outbox
  -> Kafka email.imported.v1
  -> AI classification and extraction
  -> calendar event/chip update
  -> Kafka user.activity.v1
  -> authenticated WebSocket
  -> React UI
```

Webhooks are the immediate-delivery path. Incremental polling remains enabled as
a fallback for disconnected, expired or unhealthy webhooks, and as a slower
reconciliation safety net for healthy subscriptions.

## Requirements

- Docker Desktop with Docker Compose v2
- A public HTTPS address for Gmail/Outlook webhook delivery
- OAuth credentials for each provider you enable
- An OpenAI or Gemini key, or a separately reachable Ollama service

## Configure

From PowerShell:

```powershell
cd E:\my_project\auto-ai-email-checker

if (-not (Test-Path .env)) {
    Copy-Item .env.docker.example .env
}

notepad .env
```

Do not commit `.env`. Important settings include:

```dotenv
POSTGRES_PASSWORD=replace-with-a-strong-password
SESSION_SECRET=replace-with-a-long-random-secret
TOKEN_ENCRYPTION_KEY=replace-with-a-fernet-key

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GMAIL_PUBSUB_TOPIC=
GMAIL_PUBSUB_AUDIENCE=https://mail.example.com/api/webhooks/gmail

MICROSOFT_CLIENT_ID=
MICROSOFT_CLIENT_SECRET=
MICROSOFT_WEBHOOK_CLIENT_STATE=

WEBHOOK_BASE_URL=https://mail.example.com
BACKEND_PUBLIC_URL=https://mail.example.com
FRONTEND_ORIGIN=https://mail.example.com
GOOGLE_REDIRECT_URI=https://mail.example.com/api/auth/google/callback
MICROSOFT_REDIRECT_URI=https://mail.example.com/api/auth/microsoft/callback
```

The values in `.env.docker.example` document every Docker setting.

## Start

```powershell
docker compose up --build -d
docker compose ps
```

Open:

```text
http://localhost:8080
```

Health check:

```powershell
Invoke-WebRequest http://localhost:8080/api/health
```

Follow service logs:

```powershell
docker compose logs -f email-api email-worker kafka
```

## Stop and restart

Stop containers while preserving PostgreSQL and Kafka volumes:

```powershell
docker compose down
```

Restart:

```powershell
docker compose up -d
```

Rebuild after source or dependency changes:

```powershell
docker compose up --build -d
```

Delete containers and all Docker-managed application data only when you
intentionally want a fresh installation:

```powershell
docker compose down -v
```

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
3. Grant delegated `Mail.ReadWrite`, `User.Read`, `openid`, `profile`, `email`
   and `offline_access` permissions.
4. Set a long random `MICROSOFT_WEBHOOK_CLIENT_STATE` value.

## Operational endpoints

```text
GET /api/health
GET /api/health/ready
GET /api/health/events
GET /api/activity?after_sequence=0
WS  /api/ws/activity
```

## Repository layout

```text
backend/                 FastAPI API, workers, integrations and tests
frontend/                React UI and Nginx production configuration
compose.yml              Complete runtime definition
.env.docker.example      Environment template
.dockerignore            Docker build exclusions
```
