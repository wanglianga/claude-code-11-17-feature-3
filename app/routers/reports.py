"""报告接口：批次结论生成、报告提交/批准/撤回。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require
from app.batch import evaluate_batch
from app.db import get_db
from app.models import BatchConclusion, Report
from app.reports import ReportError, approve_report, submit_report, withdraw_report
from app.schemas import (
    BatchConclusionOut,
    ReportOut,
    ReportSubmitRequest,
    WithdrawRequest,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.post("/batch-conclusions", response_model=BatchConclusionOut)
def generate_conclusion(
    body: ReportSubmitRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    record = evaluate_batch(db, body.batch_no)
    db.commit()
    db.refresh(record)
    return record


@router.get("/batch-conclusions/{batch_no}", response_model=list[BatchConclusionOut])
def list_conclusions(
    batch_no: str,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    return (
        db.query(BatchConclusion)
        .filter_by(batch_no=batch_no)
        .order_by(BatchConclusion.version)
        .all()
    )


@router.post("/submit", response_model=ReportOut)
def submit(
    body: ReportSubmitRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("submit")),
):
    try:
        report = submit_report(db, body.batch_no, inspector=role,
                               conclusion_id=body.batch_conclusion_id)
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(report)
    return report


@router.post("/{report_id}/approve", response_model=ReportOut)
def approve(
    report_id: int,
    db: Session = Depends(get_db),
    role: str = Depends(require("approve")),
):
    try:
        report = approve_report(db, report_id, reviewer=role)
    except ReportError as exc:
        # 版本漂移打回属于业务拒绝而非服务异常
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    db.commit()
    db.refresh(report)
    return report


@router.post("/{report_id}/withdraw", response_model=ReportOut)
def withdraw(
    report_id: int,
    body: WithdrawRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("withdraw")),
):
    try:
        report = withdraw_report(
            db, report_id, reviewer=role, reason=body.reason,
            new_report_id=body.new_report_id,
        )
    except ReportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(report)
    return report


@router.get("", response_model=list[ReportOut])
def list_reports(
    batch_no: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(Report)
    if batch_no:
        q = q.filter(Report.batch_no == batch_no)
    if status:
        q = q.filter(Report.status == status)
    return q.order_by(Report.id).all()
