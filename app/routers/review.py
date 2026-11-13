"""复核接口：冲突列表与处理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require
from app.db import get_db
from app.models import AuditEvent, Conflict
from app.processing import resolve_conflict
from app.schemas import ConflictOut, ConflictResolveRequest

router = APIRouter(prefix="/api/review", tags=["review"])


@router.get("/conflicts", response_model=list[ConflictOut])
def list_conflicts(
    status: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(Conflict)
    if status:
        q = q.filter(Conflict.status == status)
    return q.order_by(Conflict.id).all()


@router.post("/conflicts/{conflict_id}/resolve", response_model=ConflictOut)
def resolve(
    conflict_id: int,
    body: ConflictResolveRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("review")),
):
    try:
        conflict = resolve_conflict(db, conflict_id, body.keep_result_id, role, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditEvent(
        event_type="conflict_resolved", actor=role, target=str(conflict_id),
        detail={"keep_result_id": body.keep_result_id, "note": body.note},
    ))
    db.commit()
    db.refresh(conflict)
    return conflict
