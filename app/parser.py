"""CSV / JSON 原始记录解析。

职责边界：
- 本模块只做“结构解析 + 字段级校验 + 指纹计算”，不碰数据库、不做单位换算。
- 文件级失败（编码错误、JSON 非法、CSV 缺列）抛出 FileParseError，由上层记 failed。
- 行级失败不抛异常：返回带 error_reason 的 ParsedRow，保证坏行进待处理而不是被静默丢弃。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field

# 允许的列名（随附文件用中文表头也能识别）
COLUMN_ALIASES = {
    "record_no": {"record_no", "记录编号", "来源记录号"},
    "sample_no": {"sample_no", "样品编号"},
    "sampling_location": {"sampling_location", "取样位置"},
    "analyte": {"analyte", "检测项目"},
    "method": {"method", "检测方法"},
    "reading": {"reading", "原始读数"},
    "unit": {"unit", "单位"},
    "dilution_factor": {"dilution_factor", "稀释倍数"},
    "detection_limit": {"detection_limit", "检出限"},
    "instrument_run": {"instrument_run", "仪器运行批次"},
}

REQUIRED_FIELDS = ("sample_no", "analyte", "method", "reading", "unit")

ND_TOKENS = {"nd", "n.d.", "n.d", "未检出", "低于检出限", "loq以下"}


class FileParseError(Exception):
    pass


@dataclass
class ParsedRow:
    line_no: int
    payload: dict
    row_hash: str
    error_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_reason is None


@dataclass
class ParsedFile:
    source_ref: str
    filename: str
    file_format: str
    content_hash: str
    rows: list[ParsedRow] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def parsed_rows(self) -> int:
        return sum(1 for r in self.rows if r.ok)

    @property
    def pending_rows(self) -> int:
        return sum(1 for r in self.rows if not r.ok)


def content_hash_of(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def row_hash_of(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_reading(text: str) -> tuple[float | None, str]:
    """把读数文本拆成 (数值或None, 定性前缀)。

    返回的 qualifier：exact / below / above / non_detect。
    仅做文本层面识别；区间如何保存由 processing 决定。
    """
    t = (text or "").strip()
    low = t.lower()
    if low in ND_TOKENS:
        return None, "non_detect"
    if t.startswith(("<", "≤", "<=")):
        return float(t.lstrip("<≤= ").strip()), "below"
    if t.startswith((">", "≥", ">=")):
        return float(t.lstrip(">≥= ").strip()), "above"
    return float(t), "exact"


def _normalize_key(header: str) -> str | None:
    h = header.strip()
    for canon, aliases in COLUMN_ALIASES.items():
        if h in aliases or h.lower() in aliases:
            return canon
    return None


def _coerce_float(value, field_name: str) -> float:
    if value is None or str(value).strip() == "":
        raise ValueError(f"字段 {field_name} 为空")
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"字段 {field_name} 不是合法数字：{value!r}")
    if v < 0:
        raise ValueError(f"字段 {field_name} 不能为负数：{value!r}")
    return v


def _validate_payload(payload: dict) -> str | None:
    """返回错误原因；None 表示通过。"""
    missing = [f for f in REQUIRED_FIELDS if not str(payload.get(f, "")).strip()]
    if missing:
        return f"缺少必填字段：{', '.join(missing)}"
    try:
        parse_reading(str(payload["reading"]))
    except ValueError:
        return f"原始读数无法识别：{payload['reading']!r}"
    for f in ("dilution_factor", "detection_limit"):
        raw = payload.get(f)
        if raw is not None and str(raw).strip() != "":
            try:
                _coerce_float(raw, f)
            except ValueError as exc:
                return str(exc)
    dilution = payload.get("dilution_factor")
    if dilution is not None and str(dilution).strip() != "":
        try:
            if _coerce_float(dilution, "dilution_factor") <= 0:
                return "稀释倍数必须大于 0"
        except ValueError as exc:
            return str(exc)
    return None


def _build_row(line_no: int, payload: dict) -> ParsedRow:
    clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in payload.items()}
    reason = _validate_payload(clean)
    return ParsedRow(
        line_no=line_no,
        payload=clean,
        row_hash=row_hash_of(clean),
        error_reason=reason,
    )


def parse_csv(raw_bytes: bytes, source_ref: str, filename: str) -> ParsedFile:
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileParseError(f"CSV 不是 UTF-8 编码：{exc}") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise FileParseError("CSV 文件为空，缺少表头")
    canon_keys: list[str | None] = [_normalize_key(h) for h in header]
    if "sample_no" not in canon_keys or "analyte" not in canon_keys:
        raise FileParseError(f"CSV 表头无法识别，缺少样品编号/检测项目列，实际表头：{header}")

    rows: list[ParsedRow] = []
    for idx, values in enumerate(reader, start=2):  # 行号含表头，从第 2 行起
        if not any(v.strip() for v in values):
            continue  # 跳过完全空白行，不计入总数
        payload = {}
        for key, val in zip(canon_keys, values):
            if key is not None:
                payload[key] = val
        rows.append(_build_row(idx, payload))
    return ParsedFile(source_ref, filename, "csv", content_hash_of(raw_bytes), rows)


def parse_json(raw_bytes: bytes, source_ref: str, filename: str) -> ParsedFile:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FileParseError(f"JSON 解析失败：{exc}") from exc
    if isinstance(data, dict):
        # 兼容 {"records": [...]} 包装
        records = data.get("records")
        if not isinstance(records, list):
            raise FileParseError("JSON 根对象必须是记录数组或含 records 数组")
    elif isinstance(data, list):
        records = data
    else:
        raise FileParseError("JSON 根必须是数组或对象")

    rows: list[ParsedRow] = []
    for idx, item in enumerate(records, start=1):
        if not isinstance(item, dict):
            rows.append(
                ParsedRow(idx, {"raw": item}, row_hash_of({"raw": item}),
                          f"记录不是对象：{item!r}")
            )
            continue
        payload = {k: item.get(k) for k in COLUMN_ALIASES if k in item}
        rows.append(_build_row(idx, payload))
    return ParsedFile(source_ref, filename, "json", content_hash_of(raw_bytes), rows)


def parse_file(raw_bytes: bytes, source_ref: str, filename: str) -> ParsedFile:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return parse_csv(raw_bytes, source_ref, filename)
    if lower.endswith(".json"):
        return parse_json(raw_bytes, source_ref, filename)
    raise FileParseError(f"不支持的文件类型：{filename}（仅 .csv / .json）")
