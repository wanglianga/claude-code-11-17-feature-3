"""SQLAlchemy 模型。

存储分三层，对应需求中的三类数据：
1. 采集原件：import_files / raw_records（原文与失败原因永不丢弃）
2. 标准化结果：units / decision_rules / samples / result_groups /
   standardized_results / conflicts（数值、上下界、定性描述分开）
3. 批准记录：batch_conclusions / reports / audit_events
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# ---- 常量（集中定义，避免魔法字符串散落）---------------------------------

# 样品状态
SAMPLE_VALID = "valid"
SAMPLE_INVALID = "invalid"

# 原始行解析状态
ROW_PARSED = "parsed"
ROW_PENDING = "pending"

# 标准化计算状态
CALC_OK = "standardized"              # 已完成标准化
CALC_PENDING_UNIT = "pending_unit"    # 单位量纲/基质不匹配，待处理
CALC_PENDING_MASS = "pending_mass"    # 缺样品质量/体积，无法换算总量，待补
CALC_PENDING_VALUE = "pending_value"  # 读数无法识别（如零值无检出限说明）

# 定性描述（与数值分开保存，禁止把未检出伪装成精确零值）
QUAL_QUANTIFIED = "quantified"
QUAL_BELOW_DL = "below_detection_limit"
QUAL_ABOVE_QL = "above_quantitation_limit"
QUAL_INTERVAL = "estimated_interval"

# 冲突状态
CONFLICT_OPEN = "open"
CONFLICT_RESOLVED = "resolved"

# 报告状态
REPORT_SUBMITTED = "submitted"
REPORT_APPROVED = "approved"
REPORT_WITHDRAWN = "withdrawn"

# 批次结论
BATCH_PASS = "pass"
BATCH_FAIL = "fail"
BATCH_REVIEW = "needs_review"
BATCH_HOLD = "hold"  # 前置条件不满足，不能下结论

# ---- 质控漂移监测 ----

# 告警类型：必须区分“已证实失效”与“疑似漂移”，不能一概判原料不合格
QC_ALERT_OUT_OF_RANGE = "out_of_range"  # 单次越出批准观察范围：已证实失效
QC_ALERT_DRIFT_TREND = "drift_trend"    # 连续同向/滑动均值持续偏移：疑似漂移

# 告警状态
ALERT_OPEN = "open"
ALERT_RESOLVED = "resolved"      # 主管确认关闭（仅关闭告警本身，不等于影响已解除）
ALERT_SUPERSEDED = "superseded"  # 被重算结论取代（保留历史，不擦除）

# 影响处置状态
IMPACT_OPEN = "open"
IMPACT_CLEARED = "cleared"

# 影响通知动作（通知只增不删，已发出的保留）
NOTIFY_ADDED = "added"
NOTIFY_REMOVED = "removed"

# 质控配置状态
QC_CONFIG_ACTIVE = "active"
QC_CONFIG_RETIRED = "retired"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---- 参考数据 -------------------------------------------------------------

class Unit(Base):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    unit_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 量纲：mass_fraction（质量分数类）/ mass_concentration（质量-体积浓度）/ mass / volume
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    # 换算到该量纲基准单位的乘法因子，如 ug/kg -> mg/kg 为 0.001
    factor_to_canonical: Mapped[float] = mapped_column(Numeric(18, 12), nullable=False)
    canonical_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    # 适用基质列表，如 ["solid","semi_solid"]；空列表表示不限
    applicable_matrix: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class DecisionRule(Base):
    __tablename__ = "decision_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    matrix: Mapped[str] = mapped_column(String(32), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # upper = 上限（结果不得高于），lower = 下限
    limit_type: Mapped[str] = mapped_column(String(8), nullable=False)
    limit_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    limit_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("analyte", "matrix", name="uq_rule_analyte_matrix"),
    )


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    supplier: Mapped[str] = mapped_column(String(128), nullable=False)
    material: Mapped[str] = mapped_column(String(128), nullable=False)
    matrix: Mapped[str] = mapped_column(String(32), nullable=False)
    sampling_location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 样品质量(g)/体积(mL)：换算总量时必须；缺失则相关结果停在待补
    sample_mass_g: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    sample_volume_ml: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SAMPLE_VALID)
    invalid_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---- 第一层：采集原件 ------------------------------------------------------

class ImportFile(Base):
    __tablename__ = "import_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 来源编号：同一来源的重传靠它关联
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    file_format: Mapped[str] = mapped_column(String(8), nullable=False)  # csv / json
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # imported=正常入库（可能仍含待处理行）/ duplicate=指纹重复整体跳过 / failed=文件无法解析
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    records: Mapped[list["RawRecord"]] = relationship(back_populates="import_file")

    __table_args__ = (
        UniqueConstraint("source_ref", "content_hash", name="uq_source_hash"),
    )


class RawRecord(Base):
    """行级采集原件。无论后续成败，原始 payload 都在此保留。"""

    __tablename__ = "raw_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_file_id: Mapped[int] = mapped_column(ForeignKey("import_files.id"), nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    # 来源内记录编号：同 source_ref + record_no 再次出现即“同号修订”
    record_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_no: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    analyte: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 行内容指纹：用于同号变值识别
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False, default=ROW_PARSED)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    import_file: Mapped[ImportFile] = relationship(back_populates="records")


# ---- 第二层：标准化结果 ----------------------------------------------------

class ResultGroup(Base):
    """同一（样品 × 检测项目 × 方法）的归并组，承载版本链与冲突状态。"""

    __tablename__ = "result_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(128), nullable=False)
    conflict_status: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # 无冲突时指向当前生效版本；冲突未解决时为 NULL（禁止带病出报告）
    current_result_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "standardized_results.id",
            use_alter=True,
            name="fk_group_current_result",
        ),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    results: Mapped[list["StandardizedResult"]] = relationship(
        back_populates="group", foreign_keys="StandardizedResult.group_id"
    )

    __table_args__ = (
        UniqueConstraint("sample_no", "analyte", "method", name="uq_group_key"),
    )


class StandardizedResult(Base):
    """标准化结果（版本化，不可变；修订即新增版本，旧版本标记 superseded）。"""

    __tablename__ = "standardized_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("result_groups.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    record_no: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # —— 原值回查 ——
    reading_text: Mapped[str] = mapped_column(String(64), nullable=False)
    reading_original: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    unit_original: Mapped[str] = mapped_column(String(32), nullable=False)
    dilution_factor: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=1)
    detection_limit_original: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    instrument_run: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # —— 换算链回查：[{"from":"ug/kg","to":"mg/kg","factor":0.001}, ...] ——
    conversion_chain: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    dilution_applied: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=1)
    canonical_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # —— 数值 / 区间 / 定性描述三分 ——
    # numeric_value 仅在 quantified 时有值；未检出绝不写 0
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    numeric_value: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    lower_bound: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    upper_bound: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    qualitative: Mapped[str] = mapped_column(String(48), nullable=False)

    # —— 总量（仅在样品质量/体积齐备时计算；浓度不允许擅自当总量）——
    total_mass_mg: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    total_mass_lower: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    total_mass_upper: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    # 总量是否因缺质量/体积而挂起：浓度结果仍可判定，仅总量待补
    total_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    calc_status: Mapped[str] = mapped_column(String(24), nullable=False, default=CALC_OK)
    calc_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    group: Mapped[ResultGroup] = relationship(back_populates="results", foreign_keys=[group_id])


class Conflict(Base):
    """同一项目出现不同结果：先列冲突，不按最后到达覆盖。"""

    __tablename__ = "conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("result_groups.id"), nullable=False, index=True)
    result_id_a: Mapped[int] = mapped_column(ForeignKey("standardized_results.id"), nullable=False)
    result_id_b: Mapped[int] = mapped_column(ForeignKey("standardized_results.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CONFLICT_OPEN)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_result_id: Mapped[int | None] = mapped_column(ForeignKey("standardized_results.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---- 第三层：批次结论与批准记录 --------------------------------------------

class BatchConclusion(Base):
    __tablename__ = "batch_conclusions"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    conclusion: Mapped[str] = mapped_column(String(16), nullable=False)
    # 结构性前置条件是否满足（归属明确 / 必检齐备 / 冲突已处理 / 无待补）
    prerequisites_met: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 生成时引用的结果版本快照，批准时据此重读核对
    result_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("batch_no", "version", name="uq_batch_version"),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    batch_conclusion_id: Mapped[int | None] = mapped_column(ForeignKey("batch_conclusions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REPORT_SUBMITTED)
    # 提交时固化的结果版本引用；批准前重读，版本漂移即打回重提
    result_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    payload_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    submitted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    withdrawn_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 撤回后由哪份新报告接续，形成历史链；原文不修改
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id"), nullable=True)


class AuditEvent(Base):
    """关键动作留痕：导入、修订、冲突处理、失效、提交、批准、撤回。"""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- 质控漂移监测（第四条存储线：质控原件 / 监测结论 / 影响处置分别推进）----

class QCReference(Base):
    """质控标准值配置：按（质控样 × 项目 × 方法 × 仪器）保存标准值与经批准的观察范围。

    版本化：修改配置 = 新增版本，旧版本置 retired 保留；监测评估记录所用版本，
    旧告警不被新阈值擦除。
    """

    __tablename__ = "qc_references"

    id: Mapped[int] = mapped_column(primary_key=True)
    qc_material: Mapped[str] = mapped_column(String(64), nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    # 标准值与经批准的观察范围（单位与观测值单位一致，录入时核对）
    assigned_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    range_low: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    range_high: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    # 趋势监测参数：滑动均值窗口、连续同向次数阈值、滑动均值偏差阈值
    window_size: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    mean_deviation_limit: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=QC_CONFIG_ACTIVE)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("qc_material", "analyte", "method", "instrument", "version",
                         name="uq_qc_ref_version"),
    )


class CalibrationEvent(Base):
    """校准变更：自 effective_seq 运行序号起生效，监测计算据此切段，不跨段混算。"""

    __tablename__ = "calibration_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    effective_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("instrument", "effective_seq", name="uq_calibration_point"),
    )


class InstrumentRun(Base):
    """仪器运行登记：run_id -> 仪器与真实运行顺序（run_seq）。

    样品结果的 instrument_run 通过本表获得顺序；run_seq 为空表示顺序未知，
    影响追查时列为无法定位，不能当作没有受影响样品。
    """

    __tablename__ = "instrument_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QCObservation(Base):
    """质控观测原件：无论监测结论如何，观测原文在此保留。

    run_seq 是真实运行顺序；created_at 是到达时间。迟到记录（run_seq 早、
    到达晚）插入后按 run_seq 重新排序重算。
    """

    __tablename__ = "qc_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    qc_material: Mapped[str] = mapped_column(String(64), nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    observed_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "qc_material", "analyte", "method",
                         name="uq_qc_obs_point"),
    )


class QCEvaluationRun(Base):
    """监测评估运行：每次评估留痕（触发原因、所用配置版本、逐点指标快照）。"""

    __tablename__ = "qc_evaluation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    qc_material: Mapped[str] = mapped_column(String(64), nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    config_id: Mapped[int] = mapped_column(ForeignKey("qc_references.id"), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 触发原因：observation（新观测/迟到记录）/ manual / config_change
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alerts_raised: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    alerts_superseded: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 逐观测点指标快照：[{observation_id, run_seq, deviation, consecutive, moving_avg,
    #   out_of_range, in_drift, reliable, segment_start}...]，供追查与回查
    detail: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QCAlert(Base):
    """监测结论（不可变；只推进状态）。触发数据完整保存在 trigger_data。"""

    __tablename__ = "qc_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_run_id: Mapped[int] = mapped_column(ForeignKey("qc_evaluation_runs.id"), nullable=False)
    config_id: Mapped[int] = mapped_column(ForeignKey("qc_references.id"), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    qc_material: Mapped[str] = mapped_column(String(64), nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_id: Mapped[int] = mapped_column(ForeignKey("qc_observations.id"), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(24), nullable=False)
    segment_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 触发数据：观测值、标准值、偏差、连续同向次数、滑动均值、窗口数据、阈值
    trigger_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ALERT_OPEN)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolve_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImpactAssessment(Base):
    """影响处置：每条告警 × 每份受影响报告一项，逐项解除。

    解除依据（校准/复测证据）必须登记；同一报告被多条告警影响时，
    只有全部未结影响都解除，报告才恢复可批准。
    """

    __tablename__ = "impact_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("qc_alerts.id"), nullable=False, index=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), nullable=False, index=True)
    report_no: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=IMPACT_OPEN)
    trace_run_id: Mapped[int | None] = mapped_column(ForeignKey("qc_trace_runs.id"), nullable=True)
    # 产生时的触发数据摘要与受影响运行，供批准拦截时展示
    trigger_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 解除依据：recalibration（校准证据）/ retest（复测证据）/ auto_recompute（重算移出）
    clearance_evidence_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    clearance_evidence_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    clearance_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleared_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleared_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("alert_id", "report_id", name="uq_impact_alert_report"),
    )


class QCTraceRun(Base):
    """影响追查运行：窗口、受影响/无法定位清单、报告增减，全部留痕。"""

    __tablename__ = "qc_trace_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("qc_alerts.id"), nullable=False, index=True)
    reliable_observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("qc_observations.id"), nullable=True
    )
    reliable_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alert_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 窗口内运行、受影响样品/报告、无法定位运行（缺映射不等于无影响）
    window_runs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    affected: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    unlocatable: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    reports_added: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    reports_removed: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImpactNotification(Base):
    """影响通知：只增不删。重算后报告新增或移出影响清单都会留一条。"""

    __tablename__ = "impact_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    trace_run_id: Mapped[int] = mapped_column(ForeignKey("qc_trace_runs.id"), nullable=False)
    alert_id: Mapped[int] = mapped_column(ForeignKey("qc_alerts.id"), nullable=False)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), nullable=False)
    report_no: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)  # added / removed
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
