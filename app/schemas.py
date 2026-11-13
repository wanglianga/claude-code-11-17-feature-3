"""请求/响应 schema。"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field


class ImportResponse(BaseModel):
    import_file_id: int
    source_ref: str
    status: str
    total_rows: int
    parsed_rows: int
    pending_rows: int
    new_results: int = 0
    new_versions: int = 0
    duplicate_rows: int = 0
    conflicts_raised: int = 0
    pending_reasons: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class ConflictResolveRequest(BaseModel):
    keep_result_id: int
    note: str = Field(min_length=2, max_length=500)


class ConflictOut(BaseModel):
    id: int
    group_id: int
    result_id_a: int
    result_id_b: int
    reason: str
    status: str
    resolution_note: str | None = None
    resolved_by: str | None = None
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None


class ResultOut(BaseModel):
    id: int
    group_id: int
    version: int
    source_ref: str
    record_no: str | None
    reading_text: str
    unit_original: str
    dilution_factor: float
    detection_limit_original: float | None
    conversion_chain: list[dict[str, Any]]
    dilution_applied: float
    canonical_unit: str | None
    value_type: str
    numeric_value: float | None
    lower_bound: float | None
    upper_bound: float | None
    qualitative: str
    total_mass_mg: float | None
    total_pending: bool
    calc_status: str
    calc_note: str | None
    is_superseded: bool
    created_at: dt.datetime


class GroupOut(BaseModel):
    id: int
    sample_no: str
    analyte: str
    method: str
    conflict_status: str
    current_result_id: int | None
    results: list[ResultOut] = Field(default_factory=list)


class BatchConclusionOut(BaseModel):
    id: int
    batch_no: str
    version: int
    conclusion: str
    prerequisites_met: bool
    detail: dict[str, Any]
    generated_at: dt.datetime


class ReportSubmitRequest(BaseModel):
    batch_no: str
    batch_conclusion_id: int | None = None


class ReportOut(BaseModel):
    id: int
    report_no: str
    batch_no: str
    batch_conclusion_id: int | None
    status: str
    result_refs: list[dict[str, Any]]
    payload_snapshot: dict[str, Any]
    submitted_by: str
    submitted_at: dt.datetime
    reviewed_by: str | None
    reviewed_at: dt.datetime | None
    withdraw_reason: str | None
    withdrawn_by: str | None
    withdrawn_at: dt.datetime | None
    superseded_by_id: int | None


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    new_report_id: int | None = None


class InvalidateRequest(BaseModel):
    sample_no: str
    reason: str = Field(min_length=2, max_length=500)


class RawRecordOut(BaseModel):
    id: int
    import_file_id: int
    source_ref: str
    record_no: str | None
    line_no: int
    sample_no: str | None
    analyte: str | None
    method: str | None
    raw_payload: dict[str, Any]
    row_hash: str
    parse_status: str
    error_reason: str | None
    created_at: dt.datetime


class PendingOut(BaseModel):
    """待处理视图：坏行 + 待补结果，采购/复核一眼看到哪些项目还不能确定。"""
    raw_pending: list[RawRecordOut]
    result_pending: list[ResultOut]


# ---- 质控漂移监测 ----

class QCReferenceIn(BaseModel):
    qc_material: str = Field(min_length=1, max_length=64)
    analyte: str = Field(min_length=1, max_length=64)
    method: str = Field(min_length=1, max_length=128)
    instrument: str = Field(min_length=1, max_length=64)
    assigned_value: float
    range_low: float
    range_high: float
    unit: str = Field(min_length=1, max_length=32)
    window_size: int = Field(default=3, ge=2, le=20)
    run_limit: int = Field(default=3, ge=2, le=20)
    mean_deviation_limit: float = Field(gt=0)
    note: str | None = Field(default=None, max_length=500)


class QCReferenceOut(BaseModel):
    id: int
    qc_material: str
    analyte: str
    method: str
    instrument: str
    assigned_value: float
    range_low: float
    range_high: float
    unit: str
    window_size: int
    run_limit: int
    mean_deviation_limit: float
    version: int
    status: str
    note: str | None
    created_by: str
    created_at: dt.datetime


class CalibrationIn(BaseModel):
    instrument: str = Field(min_length=1, max_length=64)
    effective_seq: int
    note: str | None = Field(default=None, max_length=500)


class CalibrationOut(BaseModel):
    id: int
    instrument: str
    effective_seq: int
    note: str | None
    recorded_by: str
    created_at: dt.datetime


class RunRegisterIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    instrument: str = Field(min_length=1, max_length=64)
    run_seq: int | None = None
    note: str | None = Field(default=None, max_length=500)


class RunOut(BaseModel):
    id: int
    run_id: str
    instrument: str
    run_seq: int | None
    note: str | None
    created_at: dt.datetime


class QCObservationIn(BaseModel):
    qc_material: str = Field(min_length=1, max_length=64)
    analyte: str = Field(min_length=1, max_length=64)
    method: str = Field(min_length=1, max_length=128)
    instrument: str = Field(min_length=1, max_length=64)
    observed_value: float
    unit: str = Field(min_length=1, max_length=32)
    run_id: str = Field(min_length=1, max_length=128)
    run_seq: int
    source_ref: str | None = Field(default=None, max_length=128)
    observed_at: dt.datetime | None = None


class QCObservationBatchIn(BaseModel):
    observations: list[QCObservationIn] = Field(min_length=1)


class QCObservationOut(BaseModel):
    id: int
    qc_material: str
    analyte: str
    method: str
    instrument: str
    observed_value: float
    unit: str
    run_id: str
    run_seq: int
    source_ref: str | None
    observed_at: dt.datetime | None
    created_at: dt.datetime


class QCObservationImportOut(BaseModel):
    inserted: int
    skipped: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    evaluations: list[int]


class QCAlertOut(BaseModel):
    id: int
    evaluation_run_id: int
    config_id: int
    config_version: int
    qc_material: str
    analyte: str
    method: str
    instrument: str
    observation_id: int
    alert_type: str
    segment_start: int
    trigger_data: dict[str, Any]
    status: str
    resolved_by: str | None
    resolved_at: dt.datetime | None
    resolve_note: str | None
    created_at: dt.datetime


class QCAlertResolveIn(BaseModel):
    note: str = Field(min_length=2, max_length=500)


class QCEvaluationRunOut(BaseModel):
    id: int
    qc_material: str
    analyte: str
    method: str
    instrument: str
    config_id: int
    config_version: int
    trigger: str
    observation_count: int
    alerts_raised: list[Any]
    alerts_superseded: list[Any]
    detail: list[dict[str, Any]]
    created_at: dt.datetime


class QCMonitorRunRequest(BaseModel):
    qc_material: str | None = None
    analyte: str | None = None
    method: str | None = None
    instrument: str | None = None


class QCMonitorRunOut(BaseModel):
    evaluations: list[int]
    traced_alerts: int


class ImpactClearIn(BaseModel):
    evidence_type: Literal["recalibration", "retest", "other"]
    evidence_ref: str = Field(min_length=2, max_length=256)
    note: str = Field(min_length=2, max_length=500)


class ImpactOut(BaseModel):
    id: int
    alert_id: int
    report_id: int
    report_no: str
    batch_no: str
    status: str
    trace_run_id: int | None
    trigger_summary: dict[str, Any]
    clearance_evidence_type: str | None
    clearance_evidence_ref: str | None
    clearance_note: str | None
    cleared_by: str | None
    cleared_at: dt.datetime | None
    created_at: dt.datetime


class QCTraceRunOut(BaseModel):
    id: int
    alert_id: int
    reliable_observation_id: int | None
    reliable_seq: int | None
    alert_seq: int
    window_runs: list[dict[str, Any]]
    affected: list[dict[str, Any]]
    unlocatable: list[dict[str, Any]]
    reports_added: list[Any]
    reports_removed: list[Any]
    created_at: dt.datetime


class ImpactNotificationOut(BaseModel):
    id: int
    trace_run_id: int
    alert_id: int
    report_id: int
    report_no: str
    action: str
    note: str | None
    created_at: dt.datetime


class ReportHoldsOut(BaseModel):
    """报告当前的质控限制视图：未结限制的触发数据与解除依据。"""
    report_id: int
    report_no: str
    status: str
    open_impacts: list[ImpactOut]
    cleared_impacts: list[ImpactOut]
