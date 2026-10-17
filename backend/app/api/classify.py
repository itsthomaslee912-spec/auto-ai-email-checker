from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.session import current_user_is_admin, mailbox_scope, require_admin
from app.db import get_db
from app.models import ClassifyCorrection, EmailMessage, MailboxConnection
from app.schemas import (
    ClassifyPromptStatusOut,
    ClassifyPromptUpdateOut,
    ClassifyTrainingExampleOut,
    ClassifyTrainingPageOut,
)
from app.services.prompt_update import (
    PromptUpdateError,
    get_prompt_status,
    unused_corrections_query,
    update_prompt_from_corrections,
)

router = APIRouter(prefix="/api/classify", tags=["classify"])


def _visible_corrections(query):
    """Admins see every training example; other sign-in accounts only their own mail's."""
    if current_user_is_admin.get():
        return query
    owned_email_ids = (
        select(EmailMessage.id)
        .join(MailboxConnection, MailboxConnection.id == EmailMessage.mailbox_id)
        .where(mailbox_scope())
    )
    return query.filter(ClassifyCorrection.email_id.in_(owned_email_ids))


@router.get("/prompt", response_model=ClassifyPromptStatusOut)
def classify_prompt_status(db: Session = Depends(get_db)) -> ClassifyPromptStatusOut:
    return get_prompt_status(db)


@router.get("/evaluation")
def classification_evaluation(db: Session = Depends(get_db)) -> dict:
    """Baseline confusion matrix for the correction holdout set.

    ``previous_label`` is the classifier prediction recorded before the human
    correction; ``corrected_label`` is the held-out truth.
    """
    rows = _visible_corrections(
        db.query(ClassifyCorrection).filter(ClassifyCorrection.evaluation_holdout.is_(True))
    ).all()
    confusion: dict[str, dict[str, int]] = {}
    correct = 0
    for row in rows:
        confusion.setdefault(row.corrected_label, {})[row.previous_label] = (
            confusion.setdefault(row.corrected_label, {}).get(row.previous_label, 0) + 1
        )
        correct += int(row.previous_label == row.corrected_label)
    total = len(rows)
    return {
        "total": total,
        "accuracy": (correct / total) if total else None,
        "confusion_matrix": confusion,
        "note": "Held-out recorded predictions; excluded from prompt training and retrieval.",
    }


@router.get("/training", response_model=ClassifyTrainingPageOut)
def list_training_examples(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ClassifyTrainingPageOut:
    query = _visible_corrections(db.query(ClassifyCorrection)).order_by(ClassifyCorrection.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    unused_count = _visible_corrections(unused_corrections_query(db)).count()
    return ClassifyTrainingPageOut(
        items=[ClassifyTrainingExampleOut.model_validate(row) for row in rows],
        unused_count=unused_count,
        total=total,
    )


@router.delete("/training/unused")
def delete_unused_training_examples(db: Session = Depends(get_db)) -> dict[str, int]:
    ids = [
        row.id
        for row in _visible_corrections(
            db.query(ClassifyCorrection).filter(ClassifyCorrection.used_in_prompt_version_id.is_(None))
        ).all()
    ]
    deleted = 0
    if ids:
        deleted = db.query(ClassifyCorrection).filter(ClassifyCorrection.id.in_(ids)).delete(
            synchronize_session=False
        )
    db.commit()
    return {"deleted": deleted}


@router.delete("/training/{example_id}")
def delete_training_example(example_id: int, db: Session = Depends(get_db)) -> dict[str, int]:
    row = _visible_corrections(db.query(ClassifyCorrection)).filter(ClassifyCorrection.id == example_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Training example not found")
    if row.used_in_prompt_version_id is not None:
        raise HTTPException(status_code=409, detail="Training example was already used in a prompt update")
    db.delete(row)
    db.commit()
    return {"deleted": 1}


@router.post("/prompt/update", response_model=ClassifyPromptUpdateOut)
async def classify_prompt_update(db: Session = Depends(get_db)) -> ClassifyPromptUpdateOut:
    require_admin()
    try:
        return await update_prompt_from_corrections(db)
    except PromptUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
