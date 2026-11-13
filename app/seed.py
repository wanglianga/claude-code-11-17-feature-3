"""随附参考数据装载（幂等）。

五类随附文件：
- sample_registry.json  样品接收档案（取样归属、基质、质量/体积）
- unit_registry.json    单位表（量纲、换算因子、适用基质）
- decision_rules.json   项目判定规则（题目随附业务规则，不自行编造行业限值）
- exception_records.json 异常记录（样品失效等，带原因与来源）
- qc_references.json    质控标准值配置（标准值与经批准的观察范围、趋势阈值）
重复执行按业务唯一键 upsert，不产生重复行。
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import DecisionRule, Sample, Unit
from app.qc import upsert_reference

DEFAULT_REF_DIR = Path("data/reference")


def _read_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def seed_units(db: Session, path: Path) -> int:
    count = 0
    for row in _read_json(path):
        obj = db.query(Unit).filter_by(unit_code=row["unit_code"]).first()
        if obj is None:
            obj = Unit(unit_code=row["unit_code"])
            db.add(obj)
        obj.unit_name = row["unit_name"]
        obj.dimension = row["dimension"]
        obj.factor_to_canonical = row["factor_to_canonical"]
        obj.canonical_unit = row["canonical_unit"]
        obj.applicable_matrix = row.get("applicable_matrix", [])
        count += 1
    db.flush()
    return count


def seed_samples(db: Session, path: Path) -> int:
    count = 0
    for row in _read_json(path):
        obj = db.query(Sample).filter_by(sample_no=row["sample_no"]).first()
        if obj is None:
            obj = Sample(sample_no=row["sample_no"])
            db.add(obj)
        obj.batch_no = row["batch_no"]
        obj.supplier = row["supplier"]
        obj.material = row["material"]
        obj.matrix = row["matrix"]
        obj.sampling_location = row.get("sampling_location")
        obj.sample_mass_g = row.get("sample_mass_g")
        obj.sample_volume_ml = row.get("sample_volume_ml")
        # 档案默认有效；失效状态由异常记录专门维护，不在此覆盖
        if obj.status is None:
            obj.status = "valid"
        count += 1
    db.flush()
    return count


def seed_rules(db: Session, path: Path) -> int:
    count = 0
    for row in _read_json(path):
        obj = db.query(DecisionRule).filter_by(rule_code=row["rule_code"]).first()
        if obj is None:
            obj = DecisionRule(rule_code=row["rule_code"])
            db.add(obj)
        obj.analyte = row["analyte"]
        obj.matrix = row["matrix"]
        obj.required = row.get("required", True)
        obj.limit_type = row["limit_type"]
        obj.limit_value = row["limit_value"]
        obj.limit_unit = row["limit_unit"]
        obj.note = row.get("note")
        count += 1
    db.flush()
    return count


def apply_exception_records(db: Session, path: Path) -> list[dict]:
    """异常记录：当前支持 sample_invalid（样品失效）。

    失效后新到结果仍保存原件，但批次汇总会把该样品隔离。
    返回实际生效的异常清单，供调用方留痕。
    """
    applied: list[dict] = []
    for row in _read_json(path):
        kind = row["exception_type"]
        if kind == "sample_invalid":
            sample = db.query(Sample).filter_by(sample_no=row["sample_no"]).first()
            if sample is None:
                applied.append({**row, "applied": False, "reason": "样品档案不存在"})
                continue
            if sample.status != "invalid":
                sample.status = "invalid"
                sample.invalid_reason = row["reason"]
            applied.append({**row, "applied": True})
        else:
            applied.append({**row, "applied": False, "reason": f"未支持的异常类型 {kind}"})
    db.flush()
    return applied


def seed_qc_references(db: Session, path: Path) -> int:
    """质控标准值配置：走 upsert_reference（同参数幂等，参数变化升版本）。

    参数变化升版本后按新配置重算该组（旧告警保留），与接口行为一致。
    """
    if not path.exists():
        return 0
    from app.qc import evaluate_group

    count = 0
    for row in _read_json(path):
        _, created = upsert_reference(db, row, actor="seed")
        if created:
            evaluate_group(db, row["qc_material"], row["analyte"], row["method"],
                           row["instrument"], trigger="config_change")
        count += 1
    db.flush()
    return count


def seed_all(db: Session, ref_dir: str | Path = DEFAULT_REF_DIR) -> dict:
    ref_dir = Path(ref_dir)
    return {
        "units": seed_units(db, ref_dir / "unit_registry.json"),
        "samples": seed_samples(db, ref_dir / "sample_registry.json"),
        "rules": seed_rules(db, ref_dir / "decision_rules.json"),
        "exceptions": apply_exception_records(db, ref_dir / "exception_records.json"),
        "qc_references": seed_qc_references(db, ref_dir / "qc_references.json"),
    }
