# CLAUDE.md

Guidance for working in this repository. Keep it current when structure,
commands, or conventions change.

## What this is

A native Windows Gmail/Outlook email classifier with interview calendar
tracking. Incoming mail is OAuth-ingested, deduplicated, written to a
transactional outbox, published to RabbitMQ, classified by an LLM, projected
into calendar events, and streamed to the React UI.

`README.md` is the operator guide. This file is the contributor guide.

## Layout

```text
backend/            FastAPI API, background workers, integrations, tests
  app/
    api/            HTTP route modules
    auth/           OAuth, sessions, and pending-OAuth state
    classify/       LLM classification and outcome extraction
    email/          Gmail and Microsoft Graph clients
    realtime/       Provider subscriptions, SSE, and renewal
    services/       Sync, ingest, event bus, outbox, and job queue
    workers/        Native sync, classification, relay, and notification entry points
    config.py       Environment configuration
    db.py           SQLAlchemy engine and sessions
    models.py       ORM models
    main.py         FastAPI application
  tests/            Pytest suite
frontend/           React, Vite, and TypeScript UI
scripts/            Native Windows setup and supervision
```

## Architecture notes

- The API admits commands; native worker processes claim durable jobs and
  persist progress.
- PostgreSQL is the system of record. RabbitMQ and idempotent consumer records
  isolate asynchronous delivery and retries.
- The notification service consumes user activity independently, persists
  in-app notifications, and emits delivery-ready events for API WebSockets.
- Native startup requires a reachable RabbitMQ endpoint.
- Webhooks are the fast path; polling provides reconciliation and recovery.

## Developer commands

Backend:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format .
```

Frontend:

```powershell
Set-Location frontend
npm run lint
npm run format
npm run typecheck
npm run build
npm run dev
```

Run the full application with `run.ps1`. `run.bat` starts the local development
stack with ngrok and backend reload enabled: Uvicorn reloads the API and the
native supervisor restarts all Python worker services after `backend/app`
source changes.

## Conventions

- Backend linting and formatting use Ruff with `backend/pyproject.toml`.
- Frontend linting and formatting use ESLint and Prettier.
- Tests import `app.*` with `backend/` as the pytest root.
- Configuration belongs in `app/config.py`; document settings in `.env.example`.
- Never commit `.env` or credentials.
- Comments explain why, not what.
