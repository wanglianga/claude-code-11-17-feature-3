"""处理管线集成测试：换算链、区间保存、修订、冲突、失效隔离、批次汇总。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.batch import evaluate_batch
from app.models import (
    BATCH_HOLD,
    BATCH_PASS,
    BATCH_FAIL,
    BATCH_REVIEW,
    CONFLICT_OPEN,
    CONFLICT_RESOLVED,
    QUAL_BELOW_DL,
    QUAL_QUANTIFIED,
    Conflict,
    ImportFile,
    RawRecord,
    Report,
    ResultGroup,
    StandardizedResult,
)
from app.parser import parse_file
from app.processing import persist_import, resolve_conflict
from app.reports import ReportError, approve_report, submit_report, withdraw_report

DATA = Path(__file__).resolve().parents[1] / "data" / "raw"


def _import(db, path: str, source_ref: str):
    parsed = parse_file(Path(path).read_bytes(), source_ref, Path(path).name)
    return persist_import(db, parsed)


def _import_both(db):
    a = _import(db, DATA / "lab_a_20261105.csv", "LAB-A")
    b = _import(db, DATA / "lab_b_20261105.json", "LAB-B")
    db.commit()
    return a, b


def test_seed_reference(seeded, db_session):
    assert seeded["units"] == 6
    assert seeded["rules"] == 7
    # 异常记录已把 S-004 置为失效
    from app.models import Sample
    s4 = db_session.query(Sample).filter_by(sample_no="S-004").one()
    assert s4.status == "invalid"


def test_unit_conversion_and_dilution_traceable(seeded, db_session):
    _import_both(db_session)
    # RA-001: 18 ug/kg × 稀释10 = 180 ug/kg -> 0.18 mg/kg
    lead = (
        db_session.query(StandardizedResult)
        .join(ResultGroup, StandardizedResult.group_id == ResultGroup.id)
        .filter(ResultGroup.sample_no == "S-001", ResultGroup.analyte == "lead",
                StandardizedResult.record_no == "RA-001")
        .one()
    )
    assert float(lead.numeric_value) == pytest.approx(0.18)
    assert lead.canonical_unit == "mg/kg"
    assert float(lead.dilution_applied) == 10.0
    assert lead.conversion_chain[0]["factor"] == pytest.approx(0.001)
    # RB-006: 6 ug/L × 稀释5 = 30 ug/L -> 0.03 mg/L
    liq = (
        db_session.query(StandardizedResult)
        .filter_by(record_no="RB-006")
        .one()
    )
    assert float(liq.numeric_value) == pytest.approx(0.03)
    assert liq.canonical_unit == "mg/L"


def test_below_detection_limit_saved_as_interval_not_zero(seeded, db_session):
    _import_both(db_session)
    nd = (
        db_session.query(StandardizedResult)
        .filter_by(record_no="RA-003")
        .one()
    )
    # ND + 检出限 2 ug/kg -> 区间 [0, 0.002 mg/kg]，数值字段必须为空，绝不是 0
    assert nd.qualitative == QUAL_BELOW_DL
    assert nd.numeric_value is None
    assert float(nd.lower_bound) == 0.0
    assert float(nd.upper_bound) == pytest.approx(0.002)


def test_bad_rows_persisted_with_reasons(seeded, db_session):
    summary, _ = _import_both(db_session)
    # 13 行中 2 行结构级失败（缺单位、读数 NA），文件整体仍入库
    assert summary.total_rows == 13
    assert summary.pending_rows == 2
    bad = db_session.query(RawRecord).filter_by(parse_status="pending").count()
    assert bad == 2
    reasons = {r.line_no: r.error_reason for r in
               db_session.query(RawRecord).filter_by(parse_status="pending").all()}
    assert any("缺少必填字段" in v for v in reasons.values())
    assert any("无法识别" in v for v in reasons.values())


def test_file_fingerprint_dedup(seeded, db_session):
    path = DATA / "lab_a_20261105.csv"
    first = _import(db_session, path, "LAB-A")
    db_session.commit()
    again = _import(db_session, path, "LAB-A")
    db_session.commit()
    assert first.status == "imported"
    assert again.status == "duplicate"
    assert db_session.query(ImportFile).count() == 1


def test_same_record_revision_keeps_new_version(seeded, db_session):
    _import_both(db_session)
    # 先解决 lead 冲突，选择 LAB-A 版本
    conflict = db_session.query(Conflict).filter(
        Conflict.reason.like("%RA-001%")
    ).first()
    ra001 = db_session.query(StandardizedResult).filter_by(record_no="RA-001").one()
    resolve_conflict(db_session, conflict.id, ra001.id, "reviewer", "采信LAB-A")
    db_session.commit()

    before_versions = db_session.query(StandardizedResult).filter_by(record_no="RA-001").count()
    rev = _import(db_session, DATA / "lab_a_20261105_revision.csv", "LAB-A")
    db_session.commit()
    assert rev.status == "imported"  # 同来源、内容变了 -> 不是 duplicate
    rows = (
        db_session.query(StandardizedResult)
        .filter_by(record_no="RA-001")
        .order_by(StandardizedResult.version)
        .all()
    )
    assert len(rows) == before_versions + 1
    assert rows[-2].is_superseded is True
    assert rows[-1].is_superseded is False
    # 新值 15 ug/kg × 10 = 150 ug/kg = 0.15 mg/kg
    assert float(rows[-1].numeric_value) == pytest.approx(0.15)
    group = db_session.get(ResultGroup, rows[-1].group_id)
    assert group.current_result_id == rows[-1].id


def test_conflict_listed_not_overwritten(seeded, db_session):
    _, summary_b = _import_both(db_session)
    # lead 数值冲突 1 条；cadmium 的 RB-002 因基质不符停在 pending_unit，不产生数值冲突
    assert summary_b.conflicts_raised == 1
    open_conflicts = db_session.query(Conflict).filter_by(status=CONFLICT_OPEN).all()
    assert len(open_conflicts) == 1
    lead_group = (
        db_session.query(ResultGroup)
        .filter_by(sample_no="S-001", analyte="lead")
        .one()
    )
    # 冲突未解 -> 无生效版本，禁止带病出报告
    assert lead_group.current_result_id is None
    # 两条原件都保留
    assert len(lead_group.results) == 2


def test_resolve_conflict_restores_current(seeded, db_session):
    _import_both(db_session)
    conflict = db_session.query(Conflict).filter(Conflict.reason.like("%RA-001%")).one()
    rb = db_session.query(StandardizedResult).filter_by(record_no="RB-001").one()
    resolve_conflict(db_session, conflict.id, rb.id, "reviewer", "复测后采信LAB-B")
    db_session.commit()
    assert conflict.status == CONFLICT_RESOLVED
    group = db_session.get(ResultGroup, conflict.group_id)
    assert group.current_result_id == rb.id
    other = db_session.get(StandardizedResult, conflict.result_id_a)
    assert other.is_superseded is True


def test_matrix_mismatch_goes_pending_unit(seeded, db_session):
    # RB-002 用液体单位 mg/L 报固体样品 S-001：基质不适用，停在 pending_unit
    _import_both(db_session)
    rb002 = db_session.query(StandardizedResult).filter_by(record_no="RB-002").one()
    assert rb002.calc_status == "pending_unit"
    assert "基质" in rb002.calc_note


def test_missing_mass_holds_total_but_concentration_still_judged(seeded, db_session):
    _import_both(db_session)
    r = db_session.query(StandardizedResult).filter_by(record_no="RA-008").one()
    # S-003 无质量：总量挂起，但浓度结果仍 standardized
    assert r.total_pending is True
    assert r.total_mass_mg is None
    assert r.calc_status == "standardized"
    assert float(r.numeric_value) == pytest.approx(0.10)


def test_batch_hold_until_prerequisites_met(seeded, db_session):
    _import_both(db_session)
    conclusion = evaluate_batch(db_session, "B20261101")
    db_session.commit()
    assert conclusion.conclusion == BATCH_HOLD
    blockers = conclusion.detail["blockers"]
    # 失效样品、未决冲突、缺生效结果、待补检出限 都要被列出
    joined = " ".join(blockers)
    assert "S-004" in joined and "失效" in joined
    assert "冲突未处理" in joined
    assert "缺少必检项目" in joined
    assert "pending_value" in joined


def test_batch_liquid_pass_and_interval_review(seeded, db_session):
    _import_both(db_session)
    conclusion = evaluate_batch(db_session, "B20261102")
    db_session.commit()
    assert conclusion.conclusion == BATCH_PASS
    # 固体批次中 mercury 的 <0.06 区间横跨 0.05 限值 -> needs_review 判定可见
    solid = evaluate_batch(db_session, "B20261101")
    db_session.commit()
    mercury = next(i for i in solid.detail["items"]
                   if i["sample_no"] == "S-001" and i["analyte"] == "mercury")
    assert mercury["verdict"] == "review"
    lead_s002 = next(i for i in solid.detail["items"]
                     if i["sample_no"] == "S-002" and i["analyte"] == "lead")
    assert lead_s002["verdict"] == "nonconform"


def test_full_resolution_then_batch_fail(seeded, db_session):
    """冲突全部处理、待补补齐后，含超标项的批次结论应为 fail。"""
    _import_both(db_session)
    # 解决两个冲突：lead 采信 LAB-A(0.18)，cadmium 冲突涉及 pending_unit 的 RB-002，
    # 只能采信已标准化的 RA-002
    for c in db_session.query(Conflict).filter_by(status=CONFLICT_OPEN).all():
        keep = next(
            (r for r in (db_session.get(StandardizedResult, c.result_id_a),
                         db_session.get(StandardizedResult, c.result_id_b))
             if r.calc_status == "standardized"),
            None,
        )
        resolve_conflict(db_session, c.id, keep.id, "reviewer", "测试处理")
    # RA-007 未检出缺检出限：补一条带检出限的同号修订
    fixed_csv = (
        "record_no,sample_no,analyte,method,reading,unit,dilution_factor,detection_limit\n"
        "RA-007,S-002,aflatoxin_b1,HPLC-FLD,ND,ug/kg,1,2\n"
    ).encode("utf-8")
    parsed = parse_file(fixed_csv, "LAB-A-FIX", "fix.csv")
    persist_import(db_session, parsed)
    db_session.commit()

    conclusion = evaluate_batch(db_session, "B20261101")
    db_session.commit()
    # S-004 失效仍是硬阻断
    assert conclusion.conclusion == BATCH_HOLD
    assert any("S-004" in b for b in conclusion.detail["blockers"])


def test_report_submit_approve_withdraw_history(seeded, db_session):
    _import_both(db_session)
    conclusion = evaluate_batch(db_session, "B20261102")
    db_session.commit()
    # 液体批次前置条件满足，可以走完整批准流
    report = submit_report(db_session, "B20261102", "inspector", conclusion.id)
    db_session.commit()
    approved = approve_report(db_session, report.id, "reviewer")
    db_session.commit()
    assert approved.status == "approved"

    # 再来一份新报告，撤回旧报告并关联，旧报告原文不改
    conclusion2 = evaluate_batch(db_session, "B20261102")
    db_session.commit()
    new_report = submit_report(db_session, "B20261102", "inspector", conclusion2.id)
    db_session.commit()
    withdrawn = withdraw_report(
        db_session, report.id, "reviewer", "来源修订，撤回重签", new_report.id
    )
    db_session.commit()
    assert withdrawn.status == "withdrawn"
    assert withdrawn.superseded_by_id == new_report.id
    assert new_report.status == "submitted"  # 新报告独立留痕
    # 历史报告数量保留
    assert db_session.query(Report).filter_by(batch_no="B20261102").count() == 2


def test_approval_drift_rejects(seeded, db_session):
    """复核与新修订同时到达：批准前重读发现版本漂移，必须打回。"""
    _import_both(db_session)
    conclusion = evaluate_batch(db_session, "B20261102")
    db_session.commit()
    report = submit_report(db_session, "B20261102", "inspector", conclusion.id)
    db_session.commit()

    # 提交后、批准前，S-101 lead 出现同号修订（值变化）
    revised = (
        '{"records":[{"record_no":"RB-006","sample_no":"S-101","analyte":"lead",'
        '"method":"ICP-MS","reading":"8","unit":"ug/L","dilution_factor":5,'
        '"detection_limit":1,"instrument_run":"RUN-2210-R"}]}'
    ).encode("utf-8")
    persist_import(db_session, parse_file(revised, "LAB-B", "rev.json"))
    db_session.commit()

    with pytest.raises(ReportError, match="版本漂移"):
        approve_report(db_session, report.id, "reviewer")
    db_session.rollback()


def test_invalid_sample_new_result_isolated(seeded, db_session):
    """失效样品的新结果仍保存原件，但不参与有效报告。"""
    _import_both(db_session)
    new_row = (
        "record_no,sample_no,analyte,method,reading,unit,dilution_factor\n"
        "RA-100,S-004,lead,ICP-MS,0.01,mg/kg,1\n"
    ).encode("utf-8")
    persist_import(db_session, parse_file(new_row, "LAB-A", "s004.csv"))
    db_session.commit()
    r = db_session.query(StandardizedResult).filter_by(record_no="RA-100").one()
    assert r.calc_status == "standardized"  # 原件正常标准化
    assert "失效" in r.calc_note
    conclusion = evaluate_batch(db_session, "B20261101")
    db_session.commit()
    assert any("S-004" in b and "失效" in b for b in conclusion.detail["blockers"])
