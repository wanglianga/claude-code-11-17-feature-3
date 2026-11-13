"""区间判定测试：全区间符合/不符合/横跨边界交复核。"""
from __future__ import annotations

from types import SimpleNamespace

from app.judgment import (
    VERDICT_CONFORM,
    VERDICT_NONCONFORM,
    VERDICT_REVIEW,
    judge_interval,
)

UPPER_RULE = SimpleNamespace(
    rule_code="R-TEST", limit_type="upper", limit_value=0.05, limit_unit="mg/kg"
)
LOWER_RULE = SimpleNamespace(
    rule_code="R-TEST-LOW", limit_type="lower", limit_value=0.5, limit_unit="mg/kg"
)


def test_entire_interval_below_limit_is_conform():
    # 未检出 [0, 0.002] 对上限 0.05：全区间符合
    j = judge_interval(0.0, 0.002, UPPER_RULE)
    assert j.verdict == VERDICT_CONFORM


def test_entire_interval_above_limit_is_nonconform():
    j = judge_interval(0.25, 0.25, UPPER_RULE)
    assert j.verdict == VERDICT_NONCONFORM


def test_interval_straddling_limit_goes_to_review():
    # [0.01, 0.06] 横跨 0.05：不得猜成合格或不合格
    j = judge_interval(0.01, 0.06, UPPER_RULE)
    assert j.verdict == VERDICT_REVIEW


def test_exact_value_on_boundary_is_conform():
    # 闭区间，恰好等于上限视为符合
    j = judge_interval(0.05, 0.05, UPPER_RULE)
    assert j.verdict == VERDICT_CONFORM


def test_lower_limit_rule_symmetric():
    assert judge_interval(0.6, 0.8, LOWER_RULE).verdict == VERDICT_CONFORM
    assert judge_interval(0.1, 0.4, LOWER_RULE).verdict == VERDICT_NONCONFORM
    assert judge_interval(0.4, 0.6, LOWER_RULE).verdict == VERDICT_REVIEW
