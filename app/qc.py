"""质控样漂移监测与受影响报告追查。

设计要点（对应需求逐条落地）：
1. 每组质控样按（质控样 × 项目 × 方法 × 仪器）保存标准值与经批准的观察范围，
   配置版本化：修改 = 新版本，旧版本 retired 保留，旧告警不被新阈值擦除。
2. 按真实运行顺序（run_seq）计算偏差、连续同向次数、滑动均值；校准变更切段，
   两段不同条件不混算（段内重置趋势状态）。
3. 单次越界 = 已证实失效（out_of_range）；连续同向/滑动均值持续偏移 = 疑似漂移
   （drift_trend）。两者分开告警，疑似漂移不直接判原料不合格。
4. 迟到记录插回原顺序重算：评估按 run_seq 排序全量重算，不再成立的告警置
   superseded（保留历史），新成立的告警新增；影响通知只增不删，并说明报告增减。
5. 影响追查：定位最近一次可靠质控（在控且未处于漂移趋势的观测点）之后的仪器
   运行，沿 运行 -> 结果 -> 样品 -> 批次 -> 报告 列出影响清单；运行批次缺少
   映射或顺序未知时列为无法定位，不当作没有受影响样品。
6. 影响处置与监测结论分别推进：报告批准被未结影响暂缓，主管登记校准/复测证据
   后逐项解除；同一报告的全部未结影响都解除后才恢复可批准。
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    ALERT_OPEN,
    ALERT_RESOLVED,
    ALERT_SUPERSEDED,
    IMPACT_CLEARED,
    IMPACT_OPEN,
    NOTIFY_ADDED,
    NOTIFY_REMOVED,
    QC_ALERT_DRIFT_TREND,
    QC_ALERT_OUT_OF_RANGE,
    QC_CONFIG_ACTIVE,
    QC_CONFIG_RETIRED,
    REPORT_APPROVED,
    REPORT_SUBMITTED,
    AuditEvent,
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
    ResultGroup,
    Sample,
    StandardizedResult,
    utcnow,
)


class QCError(RuntimeError):
    pass


def _group_key(material: str, analyte: str, method: str, instrument: str) -> str:
    return f"{material}|{analyte}|{method}|{instrument}"


# ---- 配置：标准值与观察范围（版本化）---------------------------------------

def get_active_reference(
    db: Session, qc_material: str, analyte: str, method: str, instrument: str
) -> QCReference | None:
    return (
        db.query(QCReference)
        .filter_by(
            qc_material=qc_material, analyte=analyte, method=method,
            instrument=instrument, status=QC_CONFIG_ACTIVE,
        )
        .first()
    )


def upsert_reference(db: Session, payload: dict, actor: str) -> tuple[QCReference, bool]:
    """登记/修改质控配置。同参数重复提交幂等；参数变化 -> 新版本，旧版本 retired。

    返回 (配置, 是否新建版本)。新建版本后调用方应触发重算（config_change）。
    """
    current = get_active_reference(
        db, payload["qc_material"], payload["analyte"], payload["method"], payload["instrument"]
    )
    numeric_fields = (
        "assigned_value", "range_low", "range_high", "mean_deviation_limit",
    )
    literal_fields = ("unit", "window_size", "run_limit")
    unchanged = current is not None and all(
        float(getattr(current, f)) == float(payload[f]) for f in numeric_fields
    ) and all(
        getattr(current, f) == payload[f] for f in literal_fields
    )
    if unchanged:
        return current, False  # 参数一致：幂等，不产生新版本

    if current is not None:
        current.status = QC_CONFIG_RETIRED
    version = 1 if current is None else current.version + 1
    ref = QCReference(
        qc_material=payload["qc_material"],
        analyte=payload["analyte"],
        method=payload["method"],
        instrument=payload["instrument"],
        assigned_value=payload["assigned_value"],
        range_low=payload["range_low"],
        range_high=payload["range_high"],
        unit=payload["unit"],
        window_size=payload.get("window_size", 3),
        run_limit=payload.get("run_limit", 3),
        mean_deviation_limit=payload["mean_deviation_limit"],
        version=version,
        status=QC_CONFIG_ACTIVE,
        note=payload.get("note"),
        created_by=actor,
    )
    db.add(ref)
    db.flush()
    db.add(AuditEvent(
        event_type="qc_reference_upserted", actor=actor,
        target=_group_key(ref.qc_material, ref.analyte, ref.method, ref.instrument),
        detail={"version": version, "config_id": ref.id},
    ))
    return ref, True


def record_calibration(
    db: Session, instrument: str, effective_seq: int, note: str | None, actor: str
) -> CalibrationEvent:
    dup = (
        db.query(CalibrationEvent)
        .filter_by(instrument=instrument, effective_seq=effective_seq)
        .first()
    )
    if dup is not None:
        return dup  # 幂等
    event = CalibrationEvent(
        instrument=instrument, effective_seq=effective_seq, note=note, recorded_by=actor
    )
    db.add(event)
    db.add(AuditEvent(
        event_type="calibration_recorded", actor=actor, target=instrument,
        detail={"effective_seq": effective_seq, "note": note},
    ))
    db.flush()
    return event


def register_run(
    db: Session, run_id: str, instrument: str, run_seq: int | None, note: str | None
) -> InstrumentRun:
    """登记仪器运行顺序。已登记且一致 -> 幂等；顺序冲突 -> 报错（数据不一致，人工核对）。"""
    run = db.query(InstrumentRun).filter_by(run_id=run_id).first()
    if run is None:
        run = InstrumentRun(run_id=run_id, instrument=instrument, run_seq=run_seq, note=note)
        db.add(run)
        db.flush()
        return run
    if run.instrument != instrument:
        raise QCError(
            f"运行 {run_id} 已登记在仪器 {run.instrument}，与本次登记的 {instrument} 冲突"
        )
    if run_seq is not None and run.run_seq is not None and run.run_seq != run_seq:
        raise QCError(
            f"运行 {run_id} 已登记顺序 {run.run_seq}，与本次登记的 {run_seq} 冲突"
        )
    if run.run_seq is None and run_seq is not None:
        run.run_seq = run_seq  # 之前顺序未知，现在补登
    if note:
        run.note = note
    db.flush()
    return run


# ---- 质控观测录入 -----------------------------------------------------------

def record_observations(
    db: Session, items: list[dict], actor: str
) -> dict:
    """录入质控观测（原件）。迟到记录（run_seq 早于已有点）同样接收，
    随后对涉及的分组按 run_seq 全量重算。
    """
    inserted: list[QCObservation] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    for idx, item in enumerate(items):
        key = (item["qc_material"], item["analyte"], item["method"], item["instrument"])
        config = get_active_reference(db, *key)
        if config is None:
            errors.append({
                "index": idx, "run_id": item.get("run_id"),
                "reason": f"质控组 {_group_key(*key)} 没有生效的标准值配置，先登记配置再录入观测",
            })
            continue
        if item["unit"] != config.unit:
            errors.append({
                "index": idx, "run_id": item.get("run_id"),
                "reason": f"观测单位 {item['unit']} 与配置单位 {config.unit} 不一致，"
                          "请先核对单位口径",
            })
            continue
        dup = (
            db.query(QCObservation)
            .filter_by(
                run_id=item["run_id"], qc_material=item["qc_material"],
                analyte=item["analyte"], method=item["method"],
            )
            .first()
        )
        if dup is not None:
            skipped.append({"index": idx, "run_id": item["run_id"], "reason": "同一运行点的质控观测已存在，幂等跳过"})
            continue
        try:
            register_run(db, item["run_id"], item["instrument"], item["run_seq"],
                         note="质控观测自动登记")
        except QCError as exc:
            errors.append({"index": idx, "run_id": item.get("run_id"), "reason": str(exc)})
            continue
        obs = QCObservation(
            qc_material=item["qc_material"],
            analyte=item["analyte"],
            method=item["method"],
            instrument=item["instrument"],
            observed_value=item["observed_value"],
            unit=item["unit"],
            run_id=item["run_id"],
            run_seq=item["run_seq"],
            source_ref=item.get("source_ref"),
            observed_at=item.get("observed_at"),
        )
        db.add(obs)
        inserted.append(obs)
    db.flush()

    # 对涉及的分组按真实运行顺序重算（迟到记录插回原顺序）
    evaluations = []
    for key in sorted({(o.qc_material, o.analyte, o.method, o.instrument) for o in inserted}):
        ev = evaluate_group(db, *key, trigger="observation")
        if ev is not None:
            evaluations.append(ev.id)
    if inserted:
        db.add(AuditEvent(
            event_type="qc_observations_recorded", actor=actor,
            target=inserted[0].instrument,
            detail={"inserted": len(inserted), "evaluations": evaluations},
        ))
    db.flush()
    return {
        "inserted": len(inserted),
        "skipped": skipped,
        "errors": errors,
        "evaluations": evaluations,
    }


# ---- 监测评估 ---------------------------------------------------------------

def _segment_of(run_seq: int, calibrations: list[CalibrationEvent]) -> int:
    """运行序号所属的校准段起点：<= run_seq 的最近一次校准；无则 0。"""
    start = 0
    for ev in calibrations:  # 已按 effective_seq 升序
        if ev.effective_seq <= run_seq:
            start = ev.effective_seq
        else:
            break
    return start


def evaluate_group(
    db: Session, qc_material: str, analyte: str, method: str, instrument: str,
    trigger: str = "manual",
) -> QCEvaluationRun | None:
    """对一组质控观测按真实运行顺序全量评估。

    - 校准变更切段，段内独立累计连续同向与滑动均值，不跨段混算；
    - 单次越界 -> out_of_range（已证实失效），每次越界都告警；
    - 连续同向次数或滑动均值偏差首达阈值 -> drift_trend（疑似漂移），
      趋势持续期间不重复告警，同向序列中断后再次达阈值可再告警；
    - 重算后不再成立的同配置 open 告警 -> superseded（保留，不擦除）；
    - 评估后自动对同配置的 open 告警（含刚被取代的）重做影响追查；
      旧配置版本的告警不随新配置重算，其影响保持到主管逐项解除。
    """
    config = get_active_reference(db, qc_material, analyte, method, instrument)
    if config is None:
        return None
    observations = (
        db.query(QCObservation)
        .filter_by(qc_material=qc_material, analyte=analyte, method=method,
                   instrument=instrument)
        .order_by(QCObservation.run_seq, QCObservation.id)
        .all()
    )
    if not observations:
        return None  # 无观测不产出空评估
    calibrations = (
        db.query(CalibrationEvent)
        .filter_by(instrument=instrument)
        .order_by(CalibrationEvent.effective_seq)
        .all()
    )

    assigned = float(config.assigned_value)
    low, high = float(config.range_low), float(config.range_high)
    window_n = config.window_size
    run_limit = config.run_limit
    mean_limit = float(config.mean_deviation_limit)

    open_before = {
        (a.observation_id, a.alert_type): a
        for a in db.query(QCAlert).filter_by(
            qc_material=qc_material, analyte=analyte, method=method,
            instrument=instrument, config_id=config.id, status=ALERT_OPEN,
        ).all()
    }
    covered: set[tuple[int, str]] = set()
    to_fire: list[tuple[QCObservation, str, dict]] = []
    detail: list[dict] = []

    cur_seg: int | None = None
    prev_sign = 0
    consec = 0
    seg_devs: list[float] = []
    trend_active = False

    for obs in observations:
        seg = _segment_of(obs.run_seq, calibrations)
        if seg != cur_seg:
            # 校准变更：重新确定适用区间，趋势状态重置，两段条件不混算
            cur_seg = seg
            prev_sign = 0
            consec = 0
            seg_devs = []
            trend_active = False

        value = float(obs.observed_value)
        deviation = value - assigned
        sign = 1 if deviation > 0 else (-1 if deviation < 0 else 0)
        if sign == 0 or (prev_sign != 0 and sign != prev_sign):
            trend_active = False  # 同向序列中断（变号或归零），趋势解除
        if sign != 0 and sign == prev_sign:
            consec += 1
        elif sign == 0:
            consec = 0
        else:
            consec = 1
        if sign != 0:
            prev_sign = sign
        seg_devs.append(deviation)
        window = seg_devs[-window_n:]
        moving_avg = sum(window) / len(window) if len(window) >= window_n else None

        out_of_range = value < low or value > high
        trend_hit = (consec >= run_limit) or (
            moving_avg is not None and abs(moving_avg) >= mean_limit - 1e-9
        )

        base_trigger = {
            "run_id": obs.run_id,
            "run_seq": obs.run_seq,
            "observed_value": value,
            "assigned_value": assigned,
            "deviation": deviation,
            "approved_range": [low, high],
            "unit": config.unit,
            "consecutive_same_direction": consec,
            "run_limit": run_limit,
            "moving_avg_deviation": moving_avg,
            "mean_deviation_limit": mean_limit,
            "window_deviations": window,
            "window_size": window_n,
            "segment_start": cur_seg,
            "config_version": config.version,
        }
        if out_of_range:
            covered.add((obs.id, QC_ALERT_OUT_OF_RANGE))
            if (obs.id, QC_ALERT_OUT_OF_RANGE) not in open_before:
                to_fire.append((obs, QC_ALERT_OUT_OF_RANGE, {
                    **base_trigger,
                    "verdict": "confirmed_failure",
                    "explanation": f"观测值 {value:g} 越出批准观察范围 [{low:g}, {high:g}]，已证实失效",
                }))
        if trend_hit and not trend_active:
            trend_active = True
            covered.add((obs.id, QC_ALERT_DRIFT_TREND))
            if (obs.id, QC_ALERT_DRIFT_TREND) not in open_before:
                to_fire.append((obs, QC_ALERT_DRIFT_TREND, {
                    **base_trigger,
                    "verdict": "suspected_drift",
                    "explanation": "连续同向/滑动均值持续偏移但尚未越界，疑似漂移，需人工复核确认",
                }))

        detail.append({
            "observation_id": obs.id,
            "run_id": obs.run_id,
            "run_seq": obs.run_seq,
            "observed_value": value,
            "deviation": deviation,
            "consecutive_same_direction": consec,
            "moving_avg_deviation": moving_avg,
            "out_of_range": out_of_range,
            "in_drift": trend_active,
            "segment_start": cur_seg,
            # 可靠质控：在控且未处于漂移趋势中的观测点
            "reliable": (not out_of_range) and (not trend_active),
        })

    superseded: list[int] = []
    for key, alert in open_before.items():
        if key not in covered:
            alert.status = ALERT_SUPERSEDED  # 重算后不再成立：保留历史，不擦除
            superseded.append(alert.id)

    evaluation = QCEvaluationRun(
        qc_material=qc_material, analyte=analyte, method=method, instrument=instrument,
        config_id=config.id,
        config_version=config.version,
        trigger=trigger,
        observation_count=len(observations),
        alerts_raised=[],
        alerts_superseded=superseded,
        detail=detail,
    )
    db.add(evaluation)
    db.flush()

    raised_ids: list[int] = []
    for obs, alert_type, trigger_data in to_fire:
        alert = QCAlert(
            evaluation_run_id=evaluation.id,
            config_id=config.id,
            config_version=config.version,
            qc_material=qc_material, analyte=analyte, method=method, instrument=instrument,
            observation_id=obs.id,
            alert_type=alert_type,
            segment_start=trigger_data["segment_start"],
            trigger_data=trigger_data,
            status=ALERT_OPEN,
        )
        db.add(alert)
        db.flush()
        raised_ids.append(alert.id)
    evaluation.alerts_raised = raised_ids
    db.flush()

    # 监测结论与影响处置分别推进：重算同配置 open 告警与刚被取代告警的影响清单
    alerts_to_trace = (
        db.query(QCAlert)
        .filter(
            QCAlert.qc_material == qc_material,
            QCAlert.analyte == analyte,
            QCAlert.method == method,
            QCAlert.instrument == instrument,
            QCAlert.config_id == config.id,
            or_(QCAlert.status == ALERT_OPEN, QCAlert.id.in_(superseded or [-1])),
        )
        .all()
    )
    for alert in alerts_to_trace:
        trace_alert(db, alert)
    db.flush()
    return evaluation


# ---- 影响追查 ---------------------------------------------------------------

def _reports_for_batches(db: Session, batch_nos: set[str]) -> list[Report]:
    if not batch_nos:
        return []
    return (
        db.query(Report)
        .filter(
            Report.batch_no.in_(batch_nos),
            Report.status.in_([REPORT_SUBMITTED, REPORT_APPROVED]),
        )
        .all()
    )


def trace_alert(db: Session, alert: QCAlert) -> QCTraceRun:
    """定位最近一次可靠质控之后的仪器运行，沿样品与报告引用关系列影响清单。

    - 可靠点：同组评估快照中 run_seq < 告警点且 reliable 的最近观测；找不到则
      窗口延伸到全部分组历史（保守）。
    - 窗口内运行缺样品映射、或运行顺序未登记：列为无法定位，不当作没有受影响样品。
    - 影响通知只增不删：新增报告发 added，重算移出的发 removed。
    """
    obs = db.get(QCObservation, alert.observation_id)
    if obs is None:
        raise QCError(f"告警 {alert.id} 关联的质控观测不存在")
    # 窗口依据与告警同配置版本的最近一次评估：旧告警不被新阈值重算
    evaluation = (
        db.query(QCEvaluationRun)
        .filter_by(qc_material=alert.qc_material, analyte=alert.analyte,
                   method=alert.method, instrument=alert.instrument,
                   config_id=alert.config_id)
        .order_by(QCEvaluationRun.id.desc())
        .first()
    )
    detail = evaluation.detail if evaluation else []
    reliable_points = [
        d for d in detail if d["run_seq"] < obs.run_seq and d.get("reliable")
    ]
    reliable = max(reliable_points, key=lambda d: d["run_seq"], default=None)
    reliable_seq = reliable["run_seq"] if reliable else None

    runs = db.query(InstrumentRun).filter_by(instrument=alert.instrument).all()
    window_runs = [
        r for r in runs
        if r.run_seq is not None
        and (reliable_seq is None or r.run_seq > reliable_seq)
        and r.run_seq <= obs.run_seq
    ]
    unknown_runs = [r for r in runs if r.run_seq is None]

    affected: list[dict] = []
    unlocatable: list[dict] = []
    report_map: dict[int, Report] = {}

    def _collect(run: InstrumentRun, order_unknown: bool) -> None:
        results = (
            db.query(StandardizedResult)
            .filter_by(instrument_run=run.run_id, is_superseded=False)
            .all()
        )
        if not results:
            unlocatable.append({
                "run_id": run.run_id,
                "instrument": run.instrument,
                "run_seq": run.run_seq,
                "reason": "运行批次缺少样品映射，无法定位受影响样品"
                          "（不视为没有受影响样品）",
            })
            return
        for r in results:
            group = db.get(ResultGroup, r.group_id)
            sample = db.query(Sample).filter_by(sample_no=group.sample_no).first()
            batch_no = sample.batch_no if sample else None
            reports = _reports_for_batches(db, {batch_no} if batch_no else set())
            for rep in reports:
                report_map[rep.id] = rep
            affected.append({
                "run_id": run.run_id,
                "run_seq": run.run_seq,
                "order_unknown": order_unknown,
                "sample_no": group.sample_no,
                "analyte": group.analyte,
                "result_id": r.id,
                "batch_no": batch_no,
                "report_nos": [rep.report_no for rep in reports],
            })

    for run in sorted(window_runs, key=lambda r: r.run_seq):
        _collect(run, order_unknown=False)
    for run in unknown_runs:
        # 顺序未知：无法证明在窗口之外，保守纳入影响并标注
        _collect(run, order_unknown=True)

    trace = QCTraceRun(
        alert_id=alert.id,
        reliable_observation_id=reliable["observation_id"] if reliable else None,
        reliable_seq=reliable_seq,
        alert_seq=obs.run_seq,
        window_runs=[
            {"run_id": r.run_id, "run_seq": r.run_seq} for r in
            sorted(window_runs, key=lambda x: x.run_seq)
        ],
        affected=affected,
        unlocatable=unlocatable,
    )
    db.add(trace)
    db.flush()

    # 影响清单 diff：既有 open 影响 vs 本次受影响报告
    new_report_ids = set(report_map)
    existing_open = (
        db.query(ImpactAssessment)
        .filter_by(alert_id=alert.id, status=IMPACT_OPEN)
        .all()
    )
    existing_ids = {i.report_id for i in existing_open}
    added_ids = new_report_ids - existing_ids
    removed_ids = existing_ids - new_report_ids

    trigger_summary = {
        "alert_type": alert.alert_type,
        "trigger_data": alert.trigger_data,
        "reliable_seq": reliable_seq,
        "alert_seq": obs.run_seq,
        "window_runs": trace.window_runs,
    }
    for report_id in sorted(added_ids):
        report = report_map[report_id]
        impact = (
            db.query(ImpactAssessment)
            .filter_by(alert_id=alert.id, report_id=report_id)
            .first()
        )
        if impact is not None and impact.status == IMPACT_CLEARED \
                and impact.clearance_evidence_type != "auto_recompute":
            # 主管已登记证据解除：人工解除对该（告警, 报告）组合是终态，
            # 同一告警重算不得推翻人工结论；新证据须以新告警进入
            continue
        if impact is None:
            impact = ImpactAssessment(
                alert_id=alert.id, report_id=report.id, report_no=report.report_no,
                batch_no=report.batch_no, status=IMPACT_OPEN,
            )
            db.add(impact)
        else:
            # 重算移出（auto_recompute）后再次受影响：重新打开，清除旧解除依据
            impact.status = IMPACT_OPEN
            impact.clearance_evidence_type = None
            impact.clearance_evidence_ref = None
            impact.clearance_note = None
            impact.cleared_by = None
            impact.cleared_at = None
        impact.trace_run_id = trace.id
        impact.trigger_summary = trigger_summary
        db.add(ImpactNotification(
            trace_run_id=trace.id, alert_id=alert.id, report_id=report.id,
            report_no=report.report_no, action=NOTIFY_ADDED,
            note="报告新纳入质控影响清单，批准暂缓",
        ))
        trace.reports_added = [*trace.reports_added, report.report_no]
    for impact in existing_open:
        if impact.report_id in removed_ids:
            impact.status = IMPACT_CLEARED
            impact.clearance_evidence_type = "auto_recompute"
            impact.clearance_note = "重算后该报告不再处于受影响窗口，系统自动移出"
            impact.cleared_by = "system"
            impact.cleared_at = utcnow()
            db.add(ImpactNotification(
                trace_run_id=trace.id, alert_id=alert.id, report_id=impact.report_id,
                report_no=impact.report_no, action=NOTIFY_REMOVED,
                note="重算后报告移出影响清单",
            ))
            trace.reports_removed = [*trace.reports_removed, impact.report_no]
        else:
            impact.trace_run_id = trace.id
            impact.trigger_summary = trigger_summary
    db.flush()
    return trace


def retrace_open_alerts(db: Session) -> int:
    """对全部 open 告警重做影响追查（如新报告提交后，把影响挂到新报告上）。"""
    open_alerts = db.query(QCAlert).filter_by(status=ALERT_OPEN).all()
    for alert in open_alerts:
        trace_alert(db, alert)
    db.flush()
    return len(open_alerts)


# ---- 告警关闭与影响解除（分别推进）------------------------------------------

def resolve_alert(db: Session, alert_id: int, reviewer: str, note: str) -> QCAlert:
    """关闭告警本身（主管确认已知悉/处置）。

    只推进监测结论状态，不解除任何报告影响：单个告警关闭不能越过其他限制，
    报告仍须等全部未结影响逐项解除后才可批准。
    """
    alert = db.get(QCAlert, alert_id)
    if alert is None:
        raise QCError(f"告警 {alert_id} 不存在")
    if alert.status != ALERT_OPEN:
        raise QCError(f"告警 {alert_id} 当前状态为 {alert.status}，只有 open 可关闭")
    alert.status = ALERT_RESOLVED
    alert.resolved_by = reviewer
    alert.resolved_at = utcnow()
    alert.resolve_note = note
    db.add(AuditEvent(event_type="qc_alert_resolved", actor=reviewer,
                      target=str(alert_id), detail={"note": note}))
    db.flush()
    return alert


def clear_impact(
    db: Session, impact_id: int, reviewer: str,
    evidence_type: str, evidence_ref: str, note: str,
) -> ImpactAssessment:
    """主管登记校准/复测证据，逐项解除报告影响。"""
    if evidence_type not in ("recalibration", "retest", "other"):
        raise QCError("证据类型必须是 recalibration（校准）/ retest（复测）/ other")
    impact = db.get(ImpactAssessment, impact_id)
    if impact is None:
        raise QCError(f"影响记录 {impact_id} 不存在")
    if impact.status != IMPACT_OPEN:
        raise QCError(f"影响记录 {impact_id} 已解除，请勿重复操作")
    impact.status = IMPACT_CLEARED
    impact.clearance_evidence_type = evidence_type
    impact.clearance_evidence_ref = evidence_ref
    impact.clearance_note = note
    impact.cleared_by = reviewer
    impact.cleared_at = utcnow()
    db.add(AuditEvent(
        event_type="qc_impact_cleared", actor=reviewer, target=impact.report_no,
        detail={"impact_id": impact_id, "alert_id": impact.alert_id,
                "evidence_type": evidence_type, "evidence_ref": evidence_ref},
    ))
    db.flush()
    return impact


def report_open_impacts(db: Session, report_id: int) -> list[ImpactAssessment]:
    """报告的未结影响限制（批准前检查：全部解除才恢复可批准）。"""
    return (
        db.query(ImpactAssessment)
        .filter_by(report_id=report_id, status=IMPACT_OPEN)
        .order_by(ImpactAssessment.id)
        .all()
    )
