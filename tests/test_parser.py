"""解析器测试：坏行带原因进待处理，不整文件静默失败。"""
from __future__ import annotations

import pytest

from app.parser import FileParseError, parse_csv, parse_json, parse_reading


def test_parse_reading_forms():
    assert parse_reading("ND") == (None, "non_detect")
    assert parse_reading("未检出") == (None, "non_detect")
    assert parse_reading("<0.06") == (pytest.approx(0.06), "below")
    assert parse_reading(">10") == (pytest.approx(10.0), "above")
    assert parse_reading("0.032") == (pytest.approx(0.032), "exact")


def test_csv_bad_rows_keep_error_reason():
    csv_bytes = (
        "record_no,sample_no,analyte,method,reading,unit,dilution_factor\n"
        "X1,S-001,lead,ICP-MS,0.1,mg/kg,1\n"
        "X2,S-001,lead,ICP-MS,,mg/kg,1\n"          # 读数缺失
        "X3,S-001,lead,ICP-MS,NA,mg/kg,1\n"        # 读数不可识别
        "X4,,lead,ICP-MS,0.1,mg/kg,1\n"            # 样品编号缺失
        "X5,S-001,lead,ICP-MS,0.1,mg/kg,0\n"       # 稀释倍数非法
    ).encode("utf-8")
    parsed = parse_csv(csv_bytes, "SRC", "t.csv")
    assert parsed.total_rows == 5
    assert parsed.parsed_rows == 1
    assert parsed.pending_rows == 4
    reasons = " ".join(r.error_reason for r in parsed.rows if r.error_reason)
    assert "原始读数无法识别" in reasons
    assert "缺少必填字段" in reasons
    assert "稀释倍数必须大于 0" in reasons
    # 坏行同样有指纹，原件可回查
    bad = next(r for r in parsed.rows if not r.ok)
    assert len(bad.row_hash) == 64


def test_json_wrapper_and_bad_item():
    payload = (
        '{"records": ['
        '{"record_no":"J1","sample_no":"S-001","analyte":"lead","method":"M",'
        '"reading":"0.1","unit":"mg/kg"},'
        '"not-an-object"'
        "]}"
    ).encode("utf-8")
    parsed = parse_json(payload, "SRC", "t.json")
    assert parsed.parsed_rows == 1
    assert parsed.pending_rows == 1
    assert "记录不是对象" in parsed.rows[1].error_reason


def test_broken_json_raises_file_error():
    with pytest.raises(FileParseError):
        parse_json(b"{not json", "SRC", "t.json")


def test_unsupported_type_raises():
    from app.parser import parse_file
    with pytest.raises(FileParseError):
        parse_file(b"x", "SRC", "t.xlsx")
