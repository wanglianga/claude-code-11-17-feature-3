"""纯函数测试：单位量纲/基质核对、换算、稀释只修正一次。"""
from __future__ import annotations

import pytest

from app.units import (
    UnitError,
    UnitSpec,
    apply_dilution_once,
    build_chain,
    resolve_conversion,
    scale_interval,
)

UNITS = {
    "mg/kg": UnitSpec("mg/kg", "mass_fraction", 1.0, "mg/kg", ("solid", "semi_solid")),
    "ug/kg": UnitSpec("ug/kg", "mass_fraction", 0.001, "mg/kg", ("solid", "semi_solid")),
    "mg/L": UnitSpec("mg/L", "mass_concentration", 1.0, "mg/L", ("liquid",)),
}


def test_ugkg_to_mgkg_factor():
    factor, dim_src, dim_dst = resolve_conversion("ug/kg", "mg/kg", "solid", UNITS)
    assert factor == pytest.approx(0.001)
    assert dim_src == dim_dst == "mass_fraction"


def test_dimension_mismatch_rejected():
    # 质量分数与质量-体积浓度量纲不同，禁止直接“数字换算”
    with pytest.raises(UnitError, match="量纲冲突"):
        resolve_conversion("mg/kg", "mg/L", "solid", UNITS)


def test_matrix_applicability_rejected():
    # mg/L 只适用于液体，套到固体基质必须拒绝
    with pytest.raises(UnitError, match="仅适用基质"):
        resolve_conversion("mg/L", "mg/L", "solid", UNITS)


def test_unknown_unit_rejected():
    with pytest.raises(UnitError, match="未知单位"):
        resolve_conversion("ppb", "mg/kg", "solid", UNITS)


def test_interval_scaling_keeps_bounds():
    # 未检出区间 [0, 2 ug/kg] 换算为 mg/kg，区间不塌缩成单点
    lo, hi = scale_interval(0.0, 2.0, 0.001)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(0.002)


def test_build_chain_traceable():
    chain = build_chain("ug/kg", "mg/kg", 0.001)
    assert chain == [{"from": "ug/kg", "to": "mg/kg", "factor": 0.001}]


def test_dilution_applied_only_once():
    first = apply_dilution_once(6.0, 5.0, already_applied=False)
    assert first == pytest.approx(30.0)
    with pytest.raises(UnitError, match="禁止重复修正"):
        apply_dilution_once(first, 5.0, already_applied=True)
    with pytest.raises(UnitError, match="非法稀释倍数"):
        apply_dilution_once(1.0, 0.0, already_applied=False)
