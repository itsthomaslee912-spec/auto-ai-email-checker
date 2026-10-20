from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import ai_settings, app_auth, auth, calendar_events, classify, emails, events, health, mailboxes, monitoring, notifications, webhooks
from app.auth.session import require_user
from app.config import get_settings
from app.db import init_db
from app.services.activity_consumer import activity_consumer_loop
from app.services.service_health import service_heartbeat_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if get_settings().run_db_init:
        init_db()
    stop_event = asyncio.Event()
    background_tasks = [
        asyncio.create_task(activity_consumer_loop(stop_event)),
        asyncio.create_task(service_heartbeat_loop(stop_event, "email-api")),
    ]
    logger.info("Auto AI Email Checker API started")
    try:
        yield
    finally:
        stop_event.set()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Auto AI Email Checker", version="0.1.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_timing(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
        if elapsed_ms >= settings.slow_request_threshold_ms:
            logger.warning(
                "Slow request method=%s path=%s status=%s duration_ms=%.1f",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
        return response

    signed_in = [Depends(require_user)]
    # Public: health, sign-in, OAuth callbacks (state-bound), provider webhooks.
    app.include_router(health.router)
    app.include_router(app_auth.router)
    app.include_router(app_auth.users_router)
    app.include_router(auth.router)
    app.include_router(webhooks.router)
    # Everything else is scoped to the signed-in account.
    app.include_router(ai_settings.router, dependencies=signed_in)
    app.include_router(mailboxes.router, dependencies=signed_in)
    app.include_router(emails.router, dependencies=signed_in)
    app.include_router(calendar_events.router, dependencies=signed_in)
    app.include_router(classify.router, dependencies=signed_in)
    app.include_router(notifications.router)
    app.include_router(events.router)
    app.include_router(monitoring.router)
    return app


app = create_app()
