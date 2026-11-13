"""导入接口：上传 CSV/JSON 原始记录、参考数据种子、失效标记。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth import require
from app.db import get_db
from app.models import AuditEvent, Sample, utcnow
from app.parser import FileParseError, parse_file
from app.processing import mark_sample_invalid, persist_import
from app.schemas import ImportResponse, InvalidateRequest
from app.seed import seed_all

router = APIRouter(prefix="/api/import", tags=["import"])


@router.post("/records", response_model=ImportResponse)
def upload_records(
    file: UploadFile = File(..., description="实验室原始记录，.csv 或 .json"),
    source_ref: str = Form(..., description="来源编号，重传同一来源请保持一致"),
    db: Session = Depends(get_db),
    role: str = Depends(require("import")),
):
    raw_bytes = file.file.read()
    try:
        parsed = parse_file(raw_bytes, source_ref=source_ref, filename=file.filename or "upload")
    except FileParseError as exc:
        # 文件级失败也登记一笔，避免“悄悄失败”
        from app.models import ImportFile
        from app.parser import content_hash_of

        failed = ImportFile(
            source_ref=source_ref,
            filename=file.filename or "upload",
            file_format="unknown",
            content_hash=content_hash_of(raw_bytes),
            status="failed",
            error_message=str(exc),
        )
        db.add(failed)
        db.add(AuditEvent(event_type="import_failed", actor=role,
                          target=source_ref, detail={"error": str(exc)}))
        db.commit()
        raise HTTPException(status_code=422, detail=f"文件解析失败：{exc}")

    summary = persist_import(db, parsed)
    db.add(AuditEvent(
        event_type="records_imported", actor=role, target=source_ref,
        detail={
            "status": summary.status,
            "total": summary.total_rows,
            "pending": summary.pending_rows,
            "conflicts": summary.conflicts_raised,
        },
    ))
    db.commit()
    return ImportResponse(
        import_file_id=summary.import_file_id,
        source_ref=summary.source_ref,
        status=summary.status,
        total_rows=summary.total_rows,
        parsed_rows=summary.parsed_rows,
        pending_rows=summary.pending_rows,
        new_results=summary.new_results,
        new_versions=summary.new_versions,
        duplicate_rows=summary.duplicate_rows,
        conflicts_raised=summary.conflicts_raised,
        pending_reasons=summary.pending_reasons,
        note=summary.note,
    )


@router.post("/reference/seed")
def load_reference_data(
    db: Session = Depends(get_db),
    role: str = Depends(require("seed")),
    ref_dir: str = "data/reference",
):
    result = seed_all(db, ref_dir)
    db.add(AuditEvent(event_type="reference_seeded", actor=role, detail=result))
    db.commit()
    return {"seeded": result}


@router.post("/samples/invalidate")
def invalidate_sample(
    body: InvalidateRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("invalidate")),
):
    try:
        sample = mark_sample_invalid(db, body.sample_no, body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.add(AuditEvent(
        event_type="sample_invalidated", actor=role, target=body.sample_no,
        detail={"reason": body.reason, "at": utcnow().isoformat()},
    ))
    db.commit()
    return {
        "sample_no": sample.sample_no,
        "status": sample.status,
        "invalid_reason": sample.invalid_reason,
    }
