"""查询接口：待处理视图、结果版本链、批次报告视图、采集原件回查。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import require
from app.db import get_db
from app.models import (
    CALC_OK,
    ImportFile,
    RawRecord,
    ResultGroup,
    Sample,
    StandardizedResult,
)
from sqlalchemy import or_
from app.schemas import GroupOut, PendingOut, RawRecordOut, ResultOut

router = APIRouter(prefix="/api/query", tags=["query"])


@router.get("/pending", response_model=PendingOut)
def pending_view(
    batch_no: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    """所有“还不能确定”的事项：解析失败的原始行 + 待补的标准化结果（含总量挂起）。"""
    raw_q = db.query(RawRecord).filter(RawRecord.parse_status == "pending")
    res_q = db.query(StandardizedResult).filter(
        or_(StandardizedResult.calc_status != CALC_OK, StandardizedResult.total_pending.is_(True))
    )
    if batch_no:
        sample_nos = [s.sample_no for s in db.query(Sample).filter_by(batch_no=batch_no).all()]
        raw_q = raw_q.filter(RawRecord.sample_no.in_(sample_nos or ["__none__"]))
        group_ids = [
            g.id for g in db.query(ResultGroup)
            .filter(ResultGroup.sample_no.in_(sample_nos or ["__none__"])).all()
        ]
        res_q = res_q.filter(StandardizedResult.group_id.in_(group_ids or [-1]))
    return {
        "raw_pending": raw_q.order_by(RawRecord.id).all(),
        "result_pending": res_q.order_by(StandardizedResult.id).all(),
    }


@router.get("/samples/{sample_no}/groups", response_model=list[GroupOut])
def sample_groups(
    sample_no: str,
    include_superseded: bool = True,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    groups = db.query(ResultGroup).filter_by(sample_no=sample_no).order_by(ResultGroup.id).all()
    out: list[GroupOut] = []
    for g in groups:
        results = g.results if include_superseded else [r for r in g.results if not r.is_superseded]
        out.append(
            GroupOut(
                id=g.id,
                sample_no=g.sample_no,
                analyte=g.analyte,
                method=g.method,
                conflict_status=g.conflict_status,
                current_result_id=g.current_result_id,
                results=[ResultOut.model_validate(r, from_attributes=True) for r in results],
            )
        )
    return out


@router.get("/batches/{batch_no}/report")
def batch_report_view(
    batch_no: str,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    """采购查看的整批视图：样品、生效结果（含区间与定性）、结论、报告状态。"""
    samples = db.query(Sample).filter_by(batch_no=batch_no).all()
    payload: dict = {"batch_no": batch_no, "samples": []}
    for s in samples:
        groups = db.query(ResultGroup).filter_by(sample_no=s.sample_no).all()
        sample_view = {
            "sample_no": s.sample_no,
            "status": s.status,
            "invalid_reason": s.invalid_reason,
            "sampling_location": s.sampling_location,
            "items": [],
        }
        for g in groups:
            current = (
                db.get(StandardizedResult, g.current_result_id)
                if g.current_result_id else None
            )
            sample_view["items"].append(
                {
                    "analyte": g.analyte,
                    "method": g.method,
                    "conflict_status": g.conflict_status,
                    "current": None
                    if current is None
                    else {
                        "result_id": current.id,
                        "version": current.version,
                        "value_type": current.value_type,
                        "numeric_value": _num(current.numeric_value),
                        "interval": [_num(current.lower_bound), _num(current.upper_bound)],
                        "qualitative": current.qualitative,
                        "unit": current.canonical_unit,
                        "calc_status": current.calc_status,
                        "calc_note": current.calc_note,
                        "conversion_chain": current.conversion_chain,
                        "dilution_applied": float(current.dilution_applied),
                    },
                }
            )
        payload["samples"].append(sample_view)

    from app.models import BatchConclusion, Report

    conclusions = (
        db.query(BatchConclusion).filter_by(batch_no=batch_no)
        .order_by(BatchConclusion.version.desc()).all()
    )
    payload["latest_conclusion"] = None if not conclusions else {
        "id": conclusions[0].id,
        "version": conclusions[0].version,
        "conclusion": conclusions[0].conclusion,
        "prerequisites_met": conclusions[0].prerequisites_met,
        "detail": conclusions[0].detail,
    }
    payload["reports"] = [
        {
            "report_no": r.report_no,
            "status": r.status,
            "reviewed_by": r.reviewed_by,
            "withdraw_reason": r.withdraw_reason,
            "superseded_by_id": r.superseded_by_id,
        }
        for r in db.query(Report).filter_by(batch_no=batch_no).order_by(Report.id).all()
    ]
    return payload


@router.get("/raw-records", response_model=list[RawRecordOut])
def raw_records(
    sample_no: str | None = None,
    source_ref: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(RawRecord)
    if sample_no:
        q = q.filter(RawRecord.sample_no == sample_no)
    if source_ref:
        q = q.filter(RawRecord.source_ref == source_ref)
    return q.order_by(RawRecord.id).all()


@router.get("/imports")
def import_files(
    source_ref: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(ImportFile)
    if source_ref:
        q = q.filter(ImportFile.source_ref == source_ref)
    return [
        {
            "id": f.id,
            "source_ref": f.source_ref,
            "filename": f.filename,
            "content_hash": f.content_hash,
            "status": f.status,
            "total_rows": f.total_rows,
            "parsed_rows": f.parsed_rows,
            "pending_rows": f.pending_rows,
            "error_message": f.error_message,
            "imported_at": f.imported_at.isoformat() if f.imported_at else None,
        }
        for f in q.order_by(ImportFile.id).all()
    ]


def _num(v) -> float | None:
    return None if v is None else float(v)
