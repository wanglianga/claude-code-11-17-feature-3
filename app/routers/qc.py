"""质控接口：标准值配置、校准登记、观测录入、监测评估、影响追查与解除。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require
from app.db import get_db
from app.models import (
    ALERT_OPEN,
    IMPACT_OPEN,
    CalibrationEvent,
    ImpactAssessment,
    ImpactNotification,
    InstrumentRun,
    QCAlert,
    QCEvaluationRun,
    QCObservation,
    QCReference,
    QCTraceRun,
    Report,
)
from app.qc import (
    QCError,
    clear_impact,
    evaluate_group,
    record_calibration,
    record_observations,
    register_run,
    report_open_impacts,
    resolve_alert,
    retrace_open_alerts,
    trace_alert,
    upsert_reference,
)
from app.schemas import (
    CalibrationIn,
    CalibrationOut,
    ImpactClearIn,
    ImpactNotificationOut,
    ImpactOut,
    QCAlertOut,
    QCAlertResolveIn,
    QCEvaluationRunOut,
    QCMonitorRunOut,
    QCMonitorRunRequest,
    QCObservationBatchIn,
    QCObservationImportOut,
    QCObservationOut,
    QCReferenceIn,
    QCReferenceOut,
    QCTraceRunOut,
    ReportHoldsOut,
    RunOut,
    RunRegisterIn,
)

router = APIRouter(prefix="/api/qc", tags=["qc"])


def _ref_out(ref: QCReference) -> QCReferenceOut:
    return QCReferenceOut(
        id=ref.id, qc_material=ref.qc_material, analyte=ref.analyte,
        method=ref.method, instrument=ref.instrument,
        assigned_value=float(ref.assigned_value), range_low=float(ref.range_low),
        range_high=float(ref.range_high), unit=ref.unit,
        window_size=ref.window_size, run_limit=ref.run_limit,
        mean_deviation_limit=float(ref.mean_deviation_limit),
        version=ref.version, status=ref.status, note=ref.note,
        created_by=ref.created_by, created_at=ref.created_at,
    )


# ---- 配置与登记 -------------------------------------------------------------

@router.post("/references", response_model=QCReferenceOut, status_code=201)
def create_reference(
    body: QCReferenceIn,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_configure")),
):
    """登记/修改质控标准值配置。参数变化产生新版本（旧版本 retired 保留），
    并自动按新配置重算该组（旧告警不被擦除）。"""
    if body.range_low >= body.range_high:
        raise HTTPException(status_code=400, detail="观察范围下限必须小于上限")
    if not (body.range_low <= body.assigned_value <= body.range_high):
        raise HTTPException(status_code=400, detail="标准值必须落在观察范围内")
    ref, created = upsert_reference(db, body.model_dump(), actor=role)
    if created:
        evaluate_group(db, ref.qc_material, ref.analyte, ref.method, ref.instrument,
                       trigger="config_change")
    db.commit()
    db.refresh(ref)
    return _ref_out(ref)


@router.get("/references", response_model=list[QCReferenceOut])
def list_references(
    status: str | None = None,
    instrument: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(QCReference)
    if status:
        q = q.filter(QCReference.status == status)
    if instrument:
        q = q.filter(QCReference.instrument == instrument)
    return [_ref_out(r) for r in q.order_by(QCReference.id).all()]


@router.post("/calibrations", response_model=CalibrationOut, status_code=201)
def create_calibration(
    body: CalibrationIn,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_configure")),
):
    """登记校准变更：自 effective_seq 起监测计算切段，不跨段混算。"""
    event = record_calibration(db, body.instrument, body.effective_seq, body.note, actor=role)
    db.commit()
    db.refresh(event)
    return event


@router.get("/calibrations", response_model=list[CalibrationOut])
def list_calibrations(
    instrument: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(CalibrationEvent)
    if instrument:
        q = q.filter(CalibrationEvent.instrument == instrument)
    return q.order_by(CalibrationEvent.instrument, CalibrationEvent.effective_seq).all()


@router.post("/runs", response_model=RunOut, status_code=201)
def register_instrument_run(
    body: RunRegisterIn,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_observe")),
):
    """登记仪器运行的真实顺序；缺顺序（run_seq 为空）的运行在追查时列为无法定位。"""
    try:
        run = register_run(db, body.run_id, body.instrument, body.run_seq, body.note)
    except QCError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    db.commit()
    db.refresh(run)
    return run


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    instrument: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(InstrumentRun)
    if instrument:
        q = q.filter(InstrumentRun.instrument == instrument)
    return q.order_by(InstrumentRun.instrument, InstrumentRun.run_seq).all()


# ---- 质控观测与监测评估 -----------------------------------------------------

@router.post("/observations", response_model=QCObservationImportOut)
def create_observations(
    body: QCObservationBatchIn,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_observe")),
):
    """批量录入质控观测（原件保留）。迟到记录按 run_seq 插回原顺序自动重算。"""
    result = record_observations(db, [o.model_dump() for o in body.observations], actor=role)
    db.commit()
    return result


@router.get("/observations", response_model=list[QCObservationOut])
def list_observations(
    instrument: str | None = None,
    analyte: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(QCObservation)
    if instrument:
        q = q.filter(QCObservation.instrument == instrument)
    if analyte:
        q = q.filter(QCObservation.analyte == analyte)
    return q.order_by(QCObservation.instrument, QCObservation.run_seq).all()


@router.post("/monitor/run", response_model=QCMonitorRunOut)
def run_monitor(
    body: QCMonitorRunRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_monitor")),
):
    """手动执行监测评估（默认全部有生效配置的分组），并重算全部 open 告警的影响。"""
    q = db.query(QCReference).filter(QCReference.status == "active")
    if body.qc_material:
        q = q.filter(QCReference.qc_material == body.qc_material)
    if body.analyte:
        q = q.filter(QCReference.analyte == body.analyte)
    if body.method:
        q = q.filter(QCReference.method == body.method)
    if body.instrument:
        q = q.filter(QCReference.instrument == body.instrument)
    evaluations = []
    for ref in q.all():
        ev = evaluate_group(db, ref.qc_material, ref.analyte, ref.method,
                            ref.instrument, trigger="manual")
        if ev is not None:
            evaluations.append(ev.id)
    traced = retrace_open_alerts(db)
    db.commit()
    return {"evaluations": evaluations, "traced_alerts": traced}


@router.get("/monitor/runs", response_model=list[QCEvaluationRunOut])
def list_evaluation_runs(
    instrument: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(QCEvaluationRun)
    if instrument:
        q = q.filter(QCEvaluationRun.instrument == instrument)
    return q.order_by(QCEvaluationRun.id).all()


# ---- 告警 -------------------------------------------------------------------

@router.get("/alerts", response_model=list[QCAlertOut])
def list_alerts(
    status: str | None = None,
    alert_type: str | None = None,
    instrument: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(QCAlert)
    if status:
        q = q.filter(QCAlert.status == status)
    if alert_type:
        q = q.filter(QCAlert.alert_type == alert_type)
    if instrument:
        q = q.filter(QCAlert.instrument == instrument)
    return q.order_by(QCAlert.id).all()


@router.get("/alerts/{alert_id}", response_model=QCAlertOut)
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    alert = db.get(QCAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"告警 {alert_id} 不存在")
    return alert


@router.post("/alerts/{alert_id}/resolve", response_model=QCAlertOut)
def resolve(
    alert_id: int,
    body: QCAlertResolveIn,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_clear")),
):
    """关闭告警本身；不解除报告影响（影响须逐项登记证据解除）。"""
    try:
        alert = resolve_alert(db, alert_id, reviewer=role, note=body.note)
    except QCError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(alert)
    return alert


@router.post("/alerts/{alert_id}/trace", response_model=QCTraceRunOut)
def trace(
    alert_id: int,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_monitor")),
):
    """对指定告警手动重做影响追查。"""
    alert = db.get(QCAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"告警 {alert_id} 不存在")
    if alert.status != ALERT_OPEN:
        raise HTTPException(status_code=400, detail=f"告警状态为 {alert.status}，仅 open 可追查")
    trace = trace_alert(db, alert)
    db.commit()
    db.refresh(trace)
    return trace


@router.get("/trace-runs", response_model=list[QCTraceRunOut])
def list_trace_runs(
    alert_id: int | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(QCTraceRun)
    if alert_id:
        q = q.filter(QCTraceRun.alert_id == alert_id)
    return q.order_by(QCTraceRun.id).all()


# ---- 影响处置 ---------------------------------------------------------------

@router.get("/impacts", response_model=list[ImpactOut])
def list_impacts(
    report_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(ImpactAssessment)
    if report_id:
        q = q.filter(ImpactAssessment.report_id == report_id)
    if status:
        q = q.filter(ImpactAssessment.status == status)
    return q.order_by(ImpactAssessment.id).all()


@router.post("/impacts/{impact_id}/clear", response_model=ImpactOut)
def clear(
    impact_id: int,
    body: ImpactClearIn,
    db: Session = Depends(get_db),
    role: str = Depends(require("qc_clear")),
):
    """主管登记校准/复测证据，逐项解除报告影响。"""
    try:
        impact = clear_impact(
            db, impact_id, reviewer=role,
            evidence_type=body.evidence_type,
            evidence_ref=body.evidence_ref, note=body.note,
        )
    except QCError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(impact)
    return impact


@router.get("/reports/{report_id}/holds", response_model=ReportHoldsOut)
def report_holds(
    report_id: int,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    """报告的质控限制视图：每项未结限制的触发数据与解除依据。"""
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"报告 {report_id} 不存在")
    impacts = (
        db.query(ImpactAssessment)
        .filter_by(report_id=report_id)
        .order_by(ImpactAssessment.id)
        .all()
    )
    open_items = [i for i in impacts if i.status == IMPACT_OPEN]
    cleared_items = [i for i in impacts if i.status != IMPACT_OPEN]
    return {
        "report_id": report.id,
        "report_no": report.report_no,
        "status": report.status,
        "open_impacts": open_items,
        "cleared_impacts": cleared_items,
    }


@router.get("/notifications", response_model=list[ImpactNotificationOut])
def list_notifications(
    report_id: int | None = None,
    alert_id: int | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    """影响通知历史（只增不删）：新增与移出都留痕。"""
    q = db.query(ImpactNotification)
    if report_id:
        q = q.filter(ImpactNotification.report_id == report_id)
    if alert_id:
        q = q.filter(ImpactNotification.alert_id == alert_id)
    return q.order_by(ImpactNotification.id).all()
