from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ClassifyCorrection
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


@router.get("/prompt", response_model=ClassifyPromptStatusOut)
def classify_prompt_status(db: Session = Depends(get_db)) -> ClassifyPromptStatusOut:
    return get_prompt_status(db)


@router.get("/training", response_model=ClassifyTrainingPageOut)
def list_training_examples(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ClassifyTrainingPageOut:
    query = db.query(ClassifyCorrection).order_by(ClassifyCorrection.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    unused_count = unused_corrections_query(db).count()
    return ClassifyTrainingPageOut(
        items=[ClassifyTrainingExampleOut.model_validate(row) for row in rows],
        unused_count=unused_count,
        total=total,
    )


@router.delete("/training/unused")
def delete_unused_training_examples(db: Session = Depends(get_db)) -> dict[str, int]:
    deleted = db.query(ClassifyCorrection).filter(
        ClassifyCorrection.used_in_prompt_version_id.is_(None)
    ).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted}


@router.delete("/training/{example_id}")
def delete_training_example(example_id: int, db: Session = Depends(get_db)) -> dict[str, int]:
    row = db.query(ClassifyCorrection).filter(ClassifyCorrection.id == example_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Training example not found")
    if row.used_in_prompt_version_id is not None:
        raise HTTPException(status_code=409, detail="Training example was already used in a prompt update")
    db.delete(row)
    db.commit()
    return {"deleted": 1}


@router.post("/prompt/update", response_model=ClassifyPromptUpdateOut)
async def classify_prompt_update(db: Session = Depends(get_db)) -> ClassifyPromptUpdateOut:
    try:
        return await update_prompt_from_corrections(db)
    except PromptUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
