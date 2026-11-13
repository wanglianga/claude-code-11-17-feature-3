"""单位量纲与换算。

设计要点：
- 每个单位登记量纲（dimension）与到基准单位的因子；换算前先核对两侧量纲一致，
  量纲不同（如 mg/kg 质量分数 vs mg/L 质量-体积浓度）直接拒绝，不做“数字换算”。
- 适用基质（applicable_matrix）必须与样品基质相容，防止把只适用于液体的单位套到固体。
- 换算对单点值和区间 [lower, upper] 走同一乘法，区间不塌缩为单点。
- 稀释修正不属于单位换算：在 processing 中且仅执行一次，这里只提供纯函数便于回查。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Unit


class UnitError(ValueError):
    """单位无法换算或不适用于当前基质。"""


@dataclass(frozen=True)
class UnitSpec:
    unit_code: str
    dimension: str
    factor_to_canonical: float
    canonical_unit: str
    applicable_matrix: tuple[str, ...]


def load_unit_registry(path: str | Path) -> list[UnitSpec]:
    """从随附单位表 JSON 读取登记信息（供首次种子化与离线测试使用）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    specs: list[UnitSpec] = []
    for row in raw:
        specs.append(
            UnitSpec(
                unit_code=row["unit_code"],
                dimension=row["dimension"],
                factor_to_canonical=float(row["factor_to_canonical"]),
                canonical_unit=row["canonical_unit"],
                applicable_matrix=tuple(row.get("applicable_matrix", [])),
            )
        )
    return specs


def get_unit_map(db: Session) -> dict[str, Unit]:
    return {u.unit_code: u for u in db.query(Unit).all()}


def _check_matrix(unit: Unit | UnitSpec, matrix: str) -> None:
    allowed = tuple(unit.applicable_matrix or [])
    if allowed and matrix not in allowed:
        raise UnitError(
            f"单位 {unit.unit_code} 仅适用基质 {list(allowed)}，当前样品基质为 {matrix}"
        )


def resolve_conversion(
    source_code: str, target_code: str, matrix: str, unit_map: dict[str, Unit | UnitSpec]
) -> tuple[float, str, str]:
    """返回 (换算因子, 源量纲, 目标量纲)。

    源单位先归一再转到目标单位；量纲不一致或基质不适用时抛 UnitError。
    """
    if source_code not in unit_map:
        raise UnitError(f"未知单位：{source_code}（单位表中未登记）")
    if target_code not in unit_map:
        raise UnitError(f"未知单位：{target_code}（单位表中未登记）")
    src, dst = unit_map[source_code], unit_map[target_code]
    # 量纲是更根本的约束，先核对；再核对基质适用性
    if src.dimension != dst.dimension:
        raise UnitError(
            f"量纲冲突：{source_code}({src.dimension}) 不能换算为 {target_code}({dst.dimension})"
        )
    _check_matrix(src, matrix)
    _check_matrix(dst, matrix)
    factor = float(src.factor_to_canonical) / float(dst.factor_to_canonical)
    return factor, src.dimension, dst.dimension


def canonical_unit_for(source_code: str, unit_map: dict[str, Unit | UnitSpec]) -> str:
    if source_code not in unit_map:
        raise UnitError(f"未知单位：{source_code}（单位表中未登记）")
    return unit_map[source_code].canonical_unit


def scale_value(value: float, factor: float) -> float:
    return value * factor


def scale_interval(lower: float, upper: float, factor: float) -> tuple[float, float]:
    lo, hi = lower * factor, upper * factor
    return (lo, hi) if lo <= hi else (hi, lo)


def build_chain(
    source_code: str, target_code: str, factor: float
) -> list[dict[str, str | float]]:
    """换算链留痕：原值、因子、目标单位都能回查。"""
    if source_code == target_code:
        return [{"from": source_code, "to": target_code, "factor": 1.0}]
    return [{"from": source_code, "to": target_code, "factor": factor}]


def apply_dilution_once(value: float, dilution_factor: float, already_applied: bool) -> float:
    """稀释修正只允许执行一次。

    原始读数是稀释后测得的浓度，回推原样浓度 = 读数 × 稀释倍数。
    若上游已修正过（already_applied=True），再次调用即报错，杜绝重复乘稀释。
    """
    if already_applied:
        raise UnitError("稀释修正已执行过，禁止重复修正")
    if dilution_factor <= 0:
        raise UnitError(f"非法稀释倍数：{dilution_factor}")
    return value * dilution_factor
