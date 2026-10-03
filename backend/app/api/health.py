from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import HealthOut
from app.services.auto_sync import get_auto_sync_status, run_auto_sync_check

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    db.execute(text("SELECT 1"))
    return HealthOut(status="ok")


@router.get("/api/auto-sync/status")
def auto_sync_status(db: Session = Depends(get_db)) -> dict:
    return get_auto_sync_status(db)


@router.post("/api/auto-sync/check")
async def auto_sync_check(db: Session = Depends(get_db)) -> dict:
    await run_auto_sync_check()
    return get_auto_sync_status(db)
