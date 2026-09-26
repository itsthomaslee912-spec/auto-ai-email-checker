from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import ai_settings, auth, classify, emails, events, health, mailboxes, webhooks
from app.config import get_settings
from app.db import init_db
from app.realtime.renewal import renewal_loop
from app.services.outcome_backfill import backfill_outcomes
from app.services.auto_sync import auto_sync_loop
from app.services.reclassify import start_all_pending_reclassify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop_event = asyncio.Event()
    task = asyncio.create_task(renewal_loop(stop_event))
    backfill_task = asyncio.create_task(backfill_outcomes(stop_event))
    auto_sync_task = asyncio.create_task(auto_sync_loop(stop_event))
    pending_reclassify_task = asyncio.create_task(start_all_pending_reclassify())
    logger.info("Auto AI Email Checker API started")
    try:
        yield
    finally:
        stop_event.set()
        await task
        await backfill_task
        await auto_sync_task
        await pending_reclassify_task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Auto AI Email Checker", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(ai_settings.router)
    app.include_router(auth.router)
    app.include_router(mailboxes.router)
    app.include_router(emails.router)
    app.include_router(classify.router)
    app.include_router(events.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
