"""原始记录 -> 标准化结果 的处理管线。

关键规则落地：
1. 文件按 (source_ref, content_hash) 去重；同号变值 -> 新版本修订；
   不同来源记录对同一项目给出不同结果 -> 列冲突，不覆盖。
2. 单位换算先核对量纲与基质；稀释修正只执行一次；原值/因子/结果全量留痕。
3. 未检出不写零：保存为区间 [0, 检出限] + 定性 below_detection_limit。
4. 缺样品质量/体积时不做浓度->总量换算，相关判定停在待补。
5. 失效样品的新结果仍保存原件并完成标准化，但不参与有效报告（batch 层过滤）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import (
    CALC_OK,
    CALC_PENDING_UNIT,
    CALC_PENDING_VALUE,
    CONFLICT_OPEN,
    QUAL_ABOVE_QL,
    QUAL_BELOW_DL,
    QUAL_INTERVAL,
    QUAL_QUANTIFIED,
    ROW_PARSED,
    ROW_PENDING,
    SAMPLE_INVALID,
    Conflict,
    ImportFile,
    RawRecord,
    ResultGroup,
    Sample,
    StandardizedResult,
    Unit,
    utcnow,
)
from app.parser import ParsedFile, ParsedRow, parse_reading
from app.units import (
    UnitError,
    apply_dilution_once,
    build_chain,
    canonical_unit_for,
    get_unit_map,
    resolve_conversion,
    scale_interval,
)

# 量纲 -> 计算总量时需要的样品计量字段
_DIM_NEED = {
    "mass_fraction": "sample_mass_g",
    "mass_concentration": "sample_volume_ml",
}


@dataclass
class ImportSummary:
    import_file_id: int
    source_ref: str
    status: str
    total_rows: int = 0
    parsed_rows: int = 0
    pending_rows: int = 0
    new_results: int = 0
    new_versions: int = 0
    duplicate_rows: int = 0
    conflicts_raised: int = 0
    pending_reasons: list[dict] = field(default_factory=list)
    note: str | None = None


def _to_float(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(str(value).strip())


def _bounds_equal(a: StandardizedResult | None, b: tuple[str, float, float | None]) -> bool:
    if a is None:
        return False
    _, low, high = b
    if a.value_type != b[0]:
        return False
    if a.lower_bound is None or abs(float(a.lower_bound) - low) > 1e-12:
        return False
    if high is None:
        return a.upper_bound is None
    return a.upper_bound is not None and abs(float(a.upper_bound) - high) <= 1e-12


def persist_import(db: Session, parsed: ParsedFile) -> ImportSummary:
    """落盘整个文件：原件先入库（含坏行），再逐行标准化。"""
    existing = (
        db.query(ImportFile)
        .filter_by(source_ref=parsed.source_ref, content_hash=parsed.content_hash)
        .first()
    )
    if existing is not None:
        return ImportSummary(
            import_file_id=existing.id,
            source_ref=parsed.source_ref,
            status="duplicate",
            note="来源编号+内容指纹完全一致，整文件重复，已跳过",
        )

    imp = ImportFile(
        source_ref=parsed.source_ref,
        filename=parsed.filename,
        file_format=parsed.file_format,
        content_hash=parsed.content_hash,
        status="imported",
        total_rows=parsed.total_rows,
        parsed_rows=parsed.parsed_rows,
        pending_rows=parsed.pending_rows,
    )
    db.add(imp)
    db.flush()

    summary = ImportSummary(
        import_file_id=imp.id,
        source_ref=parsed.source_ref,
        status="imported",
        total_rows=parsed.total_rows,
        parsed_rows=parsed.parsed_rows,
        pending_rows=parsed.pending_rows,
    )

    for row in parsed.rows:
        raw = RawRecord(
            import_file_id=imp.id,
            source_ref=parsed.source_ref,
            record_no=str(row.payload.get("record_no")) if row.payload.get("record_no") not in (None, "") else None,
            line_no=row.line_no,
            sample_no=row.payload.get("sample_no"),
            analyte=row.payload.get("analyte"),
            method=row.payload.get("method"),
            raw_payload=row.payload,
            row_hash=row.row_hash,
            parse_status=ROW_PARSED if row.ok else ROW_PENDING,
            error_reason=row.error_reason,
        )
        db.add(raw)
        db.flush()
        if not row.ok:
            summary.pending_reasons.append(
                {"line_no": row.line_no, "reason": row.error_reason, "payload": row.payload}
            )
            continue
        outcome = _standardize(db, raw, row, summary)
        if outcome == "duplicate":
            summary.duplicate_rows += 1

    db.flush()
    return summary


def _get_group(db: Session, sample_no: str, analyte: str, method: str) -> ResultGroup:
    group = (
        db.query(ResultGroup)
        .filter_by(sample_no=sample_no, analyte=analyte, method=method)
        .first()
    )
    if group is None:
        group = ResultGroup(
            sample_no=sample_no, analyte=analyte, method=method, conflict_status="none"
        )
        db.add(group)
        db.flush()
    return group


def _next_version(db: Session, group: ResultGroup) -> int:
    last = (
        db.query(StandardizedResult)
        .filter_by(group_id=group.id)
        .order_by(StandardizedResult.version.desc())
        .first()
    )
    return 1 if last is None else last.version + 1


def _standardize(
    db: Session, raw: RawRecord, row: ParsedRow, summary: ImportSummary
) -> str:
    """返回 new / new_version / duplicate / pending。"""
    p = row.payload
    sample_no = p["sample_no"]
    analyte = p["analyte"]
    method = p["method"]
    unit_code = p["unit"]
    dilution = _to_float(p.get("dilution_factor")) or 1.0
    dl_original = _to_float(p.get("detection_limit"))
    reading_text = str(p["reading"])
    sample = db.query(Sample).filter_by(sample_no=sample_no).first()

    group = _get_group(db, sample_no, analyte, method)

    # 同来源同记录号且行指纹一致：重复行，直接跳过
    dup = (
        db.query(StandardizedResult)
        .filter_by(group_id=group.id, record_no=raw.record_no)
        .join(RawRecord, StandardizedResult.raw_record_id == RawRecord.id)
        .filter(RawRecord.row_hash == row.row_hash)
        .first()
    )
    if dup is not None:
        return "duplicate"

    unit_map = get_unit_map(db)
    reading_num, qualifier = parse_reading(reading_text)

    # —— 先确定原始单位下的区间与定性类型 ——
    if qualifier == "non_detect":
        if dl_original is None:
            return _pending_result(
                db, raw, group, reading_text, unit_code, dilution, dl_original,
                p, CALC_PENDING_VALUE,
                "报告未检出但未提供检出限，无法确定结果上界，待补检出限",
                summary,
            )
        orig_low, orig_high, value_type, qualitative = 0.0, dl_original, QUAL_INTERVAL, QUAL_BELOW_DL
        reading_num = None
    elif qualifier == "below":
        orig_low, orig_high = 0.0, reading_num
        value_type, qualitative = QUAL_INTERVAL, QUAL_BELOW_DL
        reading_num = None
    elif qualifier == "above":
        orig_low, orig_high = reading_num, None
        value_type, qualitative = QUAL_INTERVAL, QUAL_ABOVE_QL
        reading_num = None
    else:
        orig_low = orig_high = reading_num
        value_type, qualitative = QUAL_QUANTIFIED, QUAL_QUANTIFIED

    # —— 稀释修正：只执行一次，对读数与检出限同步修正 ——
    corrected_low = apply_dilution_once(orig_low, dilution, False)
    corrected_high = (
        apply_dilution_once(orig_high, dilution, False) if orig_high is not None else None
    )
    corrected_dl = apply_dilution_once(dl_original, dilution, False) if dl_original is not None else None

    # —— 单位换算：量纲 + 基质核对（即使源单位就是基准单位也必须核对基质适用性）——
    matrix = sample.matrix if sample else "unknown"
    try:
        canonical = canonical_unit_for(unit_code, unit_map)
        factor, _, _ = resolve_conversion(unit_code, canonical, matrix, unit_map)
        chain = build_chain(unit_code, canonical, factor)
    except UnitError as exc:
        return _pending_result(
            db, raw, group, reading_text, unit_code, dilution, dl_original,
            p, CALC_PENDING_UNIT, f"单位换算被拒绝：{exc}", summary,
        )

    can_low, can_high = scale_interval(corrected_low, corrected_high, factor) \
        if corrected_high is not None else (corrected_low * factor, None)
    can_dl = corrected_dl * factor if corrected_dl is not None else None
    numeric_value = can_low if value_type == QUAL_QUANTIFIED else None

    # —— 总量：质量/体积缺失时只挂起总量，浓度结果本身仍然有效可判定 ——
    total_low = total_high = total_point = None
    total_pending = False
    calc_status = CALC_OK
    calc_note = None
    src_unit: Unit = unit_map[unit_code]
    need_field = _DIM_NEED.get(src_unit.dimension)
    if need_field is not None:
        measure = getattr(sample, need_field) if sample else None
        if measure is None:
            total_pending = True
            calc_note = (
                f"缺少样品{'质量(g)' if need_field == 'sample_mass_g' else '体积(mL)'}，"
                "浓度结果有效，但总量待补，不得直接把浓度换算为总量"
            )
        else:
            if src_unit.dimension == "mass_fraction":
                # mg/kg × kg(=g/1000)
                mult = float(measure) / 1000.0
            else:
                # mg/L × L(=mL/1000)
                mult = float(measure) / 1000.0
            total_low = can_low * mult
            total_high = can_high * mult if can_high is not None else None
            total_point = numeric_value * mult if numeric_value is not None else None

    if sample is not None and sample.status == SAMPLE_INVALID:
        calc_note = (calc_note + "；" if calc_note else "") + \
            f"样品已被标记失效（{sample.invalid_reason or '未注明原因'}），结果不参与有效报告"

    result = StandardizedResult(
        group_id=group.id,
        version=_next_version(db, group),
        raw_record_id=raw.id,
        source_ref=raw.source_ref,
        record_no=raw.record_no,
        reading_text=reading_text,
        reading_original=reading_num if value_type == QUAL_QUANTIFIED else None,
        unit_original=unit_code,
        dilution_factor=dilution,
        detection_limit_original=dl_original,
        instrument_run=p.get("instrument_run"),
        conversion_chain=chain,
        dilution_applied=dilution,
        canonical_unit=canonical,
        value_type=value_type,
        numeric_value=numeric_value,
        lower_bound=can_low,
        upper_bound=can_high,
        qualitative=qualitative,
        total_mass_mg=total_point,
        total_mass_lower=total_low,
        total_mass_upper=total_high,
        total_pending=total_pending,
        calc_status=calc_status,
        calc_note=calc_note,
    )
    db.add(result)
    db.flush()

    _integrate_version(db, group, result, raw, summary)
    if calc_status != CALC_OK or total_pending:
        summary.pending_reasons.append(
            {"line_no": row.line_no, "reason": calc_note, "record_no": raw.record_no}
        )
    return "new_version" if result.version > 1 else "new"


def _pending_result(
    db: Session, raw: RawRecord, group: ResultGroup, reading_text: str,
    unit_code: str, dilution: float, dl_original: float | None,
    payload: dict, status: str, note: str, summary: ImportSummary,
) -> str:
    """资料不足也要留一条标准化占位记录，状态停在待补，数值字段为空。"""
    result = StandardizedResult(
        group_id=group.id,
        version=_next_version(db, group),
        raw_record_id=raw.id,
        source_ref=raw.source_ref,
        record_no=raw.record_no,
        reading_text=reading_text,
        reading_original=None,
        unit_original=unit_code,
        dilution_factor=dilution,
        detection_limit_original=dl_original,
        instrument_run=payload.get("instrument_run"),
        conversion_chain=[],
        dilution_applied=dilution,
        canonical_unit=None,
        value_type="pending",
        numeric_value=None,
        lower_bound=None,
        upper_bound=None,
        qualitative="indeterminate",
        calc_status=status,
        calc_note=note,
    )
    db.add(result)
    db.flush()
    _integrate_version(db, group, result, raw, summary)
    summary.pending_reasons.append(
        {"line_no": raw.line_no, "reason": note, "record_no": raw.record_no}
    )
    return "pending"


def _integrate_version(
    db: Session, group: ResultGroup, result: StandardizedResult,
    raw: RawRecord, summary: ImportSummary,
) -> None:
    """把新结果接入版本链。

    规则：
    - 同 (source_ref, record_no) 再次出现且值变化 -> 修订：旧版本失效，新版本生效；
      若旧版本卷入未决冲突，则把冲突引用改挂到新版本，冲突保持开放等复核。
    - 不同来源记录对同一项目给出不同结果 -> 列冲突，不覆盖，current 置空。
    - 待补结果不抢占 current，也不触发冲突。
    """
    if result.version > 1:
        summary.new_versions += 1
    else:
        summary.new_results += 1

    # —— 修订识别：不依赖 current 指针，冲突期间同号修订也能认出 ——
    # 注意排除当前新结果自身（它已 flush，且同样携带 source_ref/record_no）
    prior_same_record = None
    if raw.record_no is not None:
        prior_same_record = (
            db.query(StandardizedResult)
            .filter(
                StandardizedResult.group_id == group.id,
                StandardizedResult.source_ref == raw.source_ref,
                StandardizedResult.record_no == raw.record_no,
                StandardizedResult.is_superseded.is_(False),
                StandardizedResult.id != result.id,
            )
            .order_by(StandardizedResult.version.desc())
            .first()
        )
    if prior_same_record is not None and prior_same_record.id != result.id:
        prior_same_record.is_superseded = True
        open_conflict = (
            db.query(Conflict)
            .filter_by(group_id=group.id, status=CONFLICT_OPEN)
            .first()
        )
        if open_conflict is not None:
            # 把冲突中指向旧版本的一侧改挂到新修订，冲突仍交复核定夺
            if open_conflict.result_id_a == prior_same_record.id:
                open_conflict.result_id_a = result.id
            elif open_conflict.result_id_b == prior_same_record.id:
                open_conflict.result_id_b = result.id
            open_conflict.reason = (
                f"同一项目出现不一致结果（其中一条已由来源修订）："
                f"{result.source_ref}/{result.record_no} 最新版本 v{result.version}="
                f"[{result.lower_bound}, {result.upper_bound}] {result.canonical_unit}"
            )
            group.conflict_status = CONFLICT_OPEN
            group.current_result_id = None
        else:
            group.current_result_id = result.id
            group.conflict_status = "none"
        return

    prior_current = (
        db.query(StandardizedResult)
        .filter_by(id=group.current_result_id)
        .first()
        if group.current_result_id
        else None
    )

    if prior_current is None:
        group.current_result_id = result.id
        return

    # 异号且都已标准化才比较；待补结果不触发冲突（本身已在 pending 列表）
    if result.calc_status != "standardized" or prior_current.calc_status != "standardized":
        # 待补结果不抢占 current；等资料补齐的版本到达再比较
        return

    signature_new = (result.value_type, float(result.lower_bound),
                     None if result.upper_bound is None else float(result.upper_bound))
    if _bounds_equal(prior_current, signature_new):
        # 数值等价（如换了单位但区间一致）：重复信息，保留原件但不改变生效版本
        result.is_superseded = True
        summary.duplicate_rows += 1
        return

    conflict = Conflict(
        group_id=group.id,
        result_id_a=prior_current.id,
        result_id_b=result.id,
        reason=(
            f"同一项目出现不一致结果："
            f"{prior_current.source_ref}/{prior_current.record_no}="
            f"[{prior_current.lower_bound}, {prior_current.upper_bound}] {prior_current.canonical_unit}"
            f" vs {result.source_ref}/{result.record_no}="
            f"[{result.lower_bound}, {result.upper_bound}] {result.canonical_unit}"
        ),
        status=CONFLICT_OPEN,
    )
    db.add(conflict)
    group.conflict_status = CONFLICT_OPEN
    group.current_result_id = None  # 冲突未解前没有生效版本，禁止带病出报告
    summary.conflicts_raised += 1


def resolve_conflict(
    db: Session, conflict_id: int, keep_result_id: int,
    reviewer: str, note: str,
) -> Conflict:
    """人工处理冲突：选定生效结果，另一条保留原件但标记 superseded。"""
    conflict = db.get(Conflict, conflict_id)
    if conflict is None:
        raise KeyError(f"冲突 {conflict_id} 不存在")
    if keep_result_id not in (conflict.result_id_a, conflict.result_id_b):
        raise ValueError("只能在冲突涉及的两条结果中选择生效版本")
    other_id = conflict.result_id_b if keep_result_id == conflict.result_id_a else conflict.result_id_a
    db.get(StandardizedResult, other_id).is_superseded = True
    kept = db.get(StandardizedResult, keep_result_id)
    kept.is_superseded = False
    group = db.get(ResultGroup, conflict.group_id)
    group.current_result_id = keep_result_id
    group.conflict_status = "resolved"
    conflict.status = "resolved"
    conflict.resolved_result_id = keep_result_id
    conflict.resolution_note = note
    conflict.resolved_by = reviewer
    conflict.resolved_at = utcnow()
    db.flush()
    return conflict


def mark_sample_invalid(db: Session, sample_no: str, reason: str) -> Sample:
    sample = db.query(Sample).filter_by(sample_no=sample_no).first()
    if sample is None:
        raise KeyError(f"样品 {sample_no} 不存在")
    sample.status = "invalid"
    sample.invalid_reason = reason
    db.flush()
    return sample
