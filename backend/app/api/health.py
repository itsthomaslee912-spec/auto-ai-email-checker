from fastapi import APIRouter, Depends
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.auth.session import require_user
from app.db import get_db
from app.config import get_settings
from app.models import OutboxEvent
from app.schemas import HealthOut
from app.services.auto_sync import get_auto_sync_status, run_auto_sync_check

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthOut)
async def health() -> HealthOut:
    # Keep this free of DB/pool work so the process supervisor does not kill
    # the API while reclassify or sync is using every connection.
    return HealthOut(status="ok")


@router.get("/api/health/ready", response_model=HealthOut)
def health_ready(db: Session = Depends(get_db)) -> HealthOut:
    db.execute(text("SELECT 1"))
    return HealthOut(status="ok")


@router.get("/api/auto-sync/status", dependencies=[Depends(require_user)])
def auto_sync_status(db: Session = Depends(get_db)) -> dict:
    return get_auto_sync_status(db)


@router.post("/api/auto-sync/check", dependencies=[Depends(require_user)])
async def auto_sync_check(db: Session = Depends(get_db)) -> dict:
    await run_auto_sync_check()
    return get_auto_sync_status(db)


@router.get("/api/health/events", dependencies=[Depends(require_user)])
def event_pipeline_health(db: Session = Depends(get_db)) -> dict:
    """Report durable event backlog without contacting Kafka on the request path."""
    rows = db.query(OutboxEvent.status, func.count(OutboxEvent.id)).group_by(
        OutboxEvent.status
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    return {
        "status": "ok",
        "kafka_configured": get_settings().kafka_enabled,
        "outbox": counts,
        "pending": counts.get("pending", 0) + counts.get("failed", 0),
    }
