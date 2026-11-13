"""区间判定。

不把未检出/精度不足的结果压成单个数字，因此判定对象是区间 [lower, upper]：
- 上限规则：整个区间都不高于限值 -> conform；整个区间都高于限值 -> nonconform；
  区间横跨限值 -> review（交人工复核，不猜）。
- 下限规则对称处理。
- 精确测量值退化为 [v, v] 的闭区间，判定逻辑保持同一套。
限值单位与结果单位先在 units 层做量纲核对与换算；本模块只在同一单位下比数字。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import DecisionRule, StandardizedResult

VERDICT_CONFORM = "conform"
VERDICT_NONCONFORM = "nonconform"
VERDICT_REVIEW = "review"
VERDICT_NO_RULE = "no_rule"
VERDICT_PENDING = "pending"  # 结果仍在待补/待处理，不能判定


@dataclass(frozen=True)
class Judgement:
    verdict: str
    rule_code: str | None
    limit_value: float | None
    limit_unit: str | None
    compared_lower: float | None
    compared_upper: float | None
    reason: str


def judge_interval(
    lower: float,
    upper: float,
    rule: DecisionRule,
) -> Judgement:
    """同一单位下，把结果区间与规则限值比较。"""
    limit = float(rule.limit_value)
    if rule.limit_type == "upper":
        if upper <= limit:
            reason = f"区间上界 {upper:g} <= 限值 {limit:g}，整个区间符合"
            verdict = VERDICT_CONFORM
        elif lower > limit:
            reason = f"区间下界 {lower:g} > 限值 {limit:g}，整个区间不符合"
            verdict = VERDICT_NONCONFORM
        else:
            reason = f"区间 [{lower:g}, {upper:g}] 横跨限值 {limit:g}，需人工复核"
            verdict = VERDICT_REVIEW
    elif rule.limit_type == "lower":
        if lower >= limit:
            reason = f"区间下界 {lower:g} >= 限值 {limit:g}，整个区间符合"
            verdict = VERDICT_CONFORM
        elif upper < limit:
            reason = f"区间上界 {upper:g} < 限值 {limit:g}，整个区间不符合"
            verdict = VERDICT_NONCONFORM
        else:
            reason = f"区间 [{lower:g}, {upper:g}] 横跨限值 {limit:g}，需人工复核"
            verdict = VERDICT_REVIEW
    else:
        raise ValueError(f"未知 limit_type：{rule.limit_type}")

    return Judgement(
        verdict=verdict,
        rule_code=rule.rule_code,
        limit_value=limit,
        limit_unit=rule.limit_unit,
        compared_lower=lower,
        compared_upper=upper,
        reason=reason,
    )


def judge_result(
    result: StandardizedResult,
    rule: DecisionRule | None,
    converted_interval: tuple[float, float] | None,
) -> Judgement:
    """对一条标准化结果给出判定。

    converted_interval 是已换算到规则单位的区间；为 None 表示无法换算到规则单位。
    """
    if result.calc_status != "standardized":
        return Judgement(
            VERDICT_PENDING, None, None, None, None, None,
            f"结果处于 {result.calc_status}，资料未齐，不能判定",
        )
    if rule is None:
        return Judgement(
            VERDICT_NO_RULE, None, None, None,
            float(result.lower_bound) if result.lower_bound is not None else None,
            float(result.upper_bound) if result.upper_bound is not None else None,
            "无适用判定规则（不擅自套用行业限值）",
        )
    if converted_interval is None:
        return Judgement(
            VERDICT_PENDING, rule.rule_code, float(rule.limit_value), rule.limit_unit,
            None, None,
            f"结果单位 {result.canonical_unit} 与规则单位 {rule.limit_unit} 量纲不一致，待处理",
        )
    lower, upper = converted_interval
    return judge_interval(lower, upper, rule)
