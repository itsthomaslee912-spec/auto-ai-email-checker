from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.session import require_admin, require_user
from app.config import get_settings
from app.db import get_db
from app.models import (
    EmailMessage,
    MailboxConnection,
    MailboxJobState,
    OutboxEvent,
    ServiceHeartbeat,
    User,
    WebhookDeliveryJob,
)
from app.timeutil import as_utc

router = APIRouter(
    prefix="/api/admin/monitoring",
    tags=["monitoring"],
    dependencies=[Depends(require_user)],
)

SERVICE_DETAILS = {
    "email-api": ("Email API", "REST, OAuth, webhooks and notification WebSockets"),
    "sync-worker": ("Sync worker", "Gmail and Microsoft mailbox ingestion"),
    "classification-worker": ("Classification worker", "AI classification and calendar extraction"),
    "event-relay": ("Event relay", "PostgreSQL outbox delivery to RabbitMQ"),
    "notification-service": (
        "Notification service",
        "Durable user notification preparation and routing",
    ),
}
STALE_AFTER = timedelta(seconds=45)


def _counts(db: Session, model, column) -> dict[str, int]:
    rows = db.query(column, func.count(model.id)).group_by(column).all()
    return {str(state): int(count) for state, count in rows}


@router.get("")
def monitoring_snapshot(db: Session = Depends(get_db)) -> dict:
    require_admin()
    now = datetime.now(timezone.utc)
    cutoff = now - STALE_AFTER
    heartbeat_rows = db.query(ServiceHeartbeat).all()
    services = []
    for service_name, (label, description) in SERVICE_DETAILS.items():
        rows = [row for row in heartbeat_rows if row.service_name == service_name]
        fresh = [row for row in rows if as_utc(row.last_seen_at) and as_utc(row.last_seen_at) >= cutoff]
        latest = max((as_utc(row.last_seen_at) for row in rows), default=None)
        services.append({
            "name": service_name,
            "label": label,
            "description": description,
            "status": "healthy" if fresh else ("stale" if rows else "offline"),
            "replicas": len(fresh),
            "last_seen_at": latest.isoformat() if latest else None,
        })

    outbox = _counts(db, OutboxEvent, OutboxEvent.status)
    webhook_jobs = _counts(db, WebhookDeliveryJob, WebhookDeliveryJob.status)
    mailbox_jobs = _counts(db, MailboxJobState, MailboxJobState.state)
    active_mailboxes = db.query(MailboxConnection).filter(
        MailboxConnection.is_active.is_(True)
    ).all()
    mailbox_issues = []
    for mailbox in active_mailboxes:
        webhook_error = mailbox.webhook.last_delivery_error if mailbox.webhook else None
        message = mailbox.last_sync_error or webhook_error
        if not message:
            continue
        mailbox_issues.append({
            "mailbox_id": mailbox.id,
            "email_address": mailbox.email_address,
            "provider": mailbox.provider,
            "message": str(message)[:300],
            "last_sync_completed_at": (
                as_utc(mailbox.last_sync_completed_at).isoformat()
                if mailbox.last_sync_completed_at else None
            ),
        })

    pending_events = outbox.get("pending", 0) + outbox.get("publishing", 0)
    failed_events = outbox.get("failed", 0)
    pending_webhooks = webhook_jobs.get("pending", 0) + webhook_jobs.get("processing", 0)
    active_jobs = sum(mailbox_jobs.get(state, 0) for state in ("queued", "starting", "running"))
    worker_unhealthy = any(item["status"] != "healthy" for item in services)
    overall = "degraded" if worker_unhealthy or failed_events or mailbox_issues else "healthy"
    relay = next(item for item in services if item["name"] == "event-relay")
    rabbitmq_configured = get_settings().rabbitmq_enabled

    return {
        "generated_at": now.isoformat(),
        "overall_status": overall,
        "services": services,
        "infrastructure": {
            "postgres": {"status": "healthy", "label": "PostgreSQL"},
            "rabbitmq": {
                "status": (
                    "disabled" if not rabbitmq_configured
                    else "healthy" if relay["status"] == "healthy" and failed_events == 0
                    else "degraded"
                ),
                "label": "RabbitMQ",
                "configured": rabbitmq_configured,
            },
        },
        "metrics": {
            "users": int(db.query(func.count(User.id)).filter(User.email.is_not(None)).scalar() or 0),
            "active_mailboxes": len(active_mailboxes),
            "emails": int(db.query(func.count(EmailMessage.id)).scalar() or 0),
            "pending_classification": int(db.query(func.count(EmailMessage.id)).filter(
                EmailMessage.classification_pending.is_(True)
            ).scalar() or 0),
        },
        "queues": {
            "active_mailbox_jobs": active_jobs,
            "pending_webhooks": pending_webhooks,
            "pending_events": pending_events,
            "failed_events": failed_events,
            "mailbox_jobs": mailbox_jobs,
            "webhook_jobs": webhook_jobs,
            "outbox": outbox,
        },
        "mailbox_issues": mailbox_issues[:25],
    }
