"""HTTP 接口测试：鉴权、导入、复核、报告流、查询视图。"""
from __future__ import annotations

from pathlib import Path

from tests.conftest import auth_header

DATA = Path(__file__).resolve().parents[1] / "data"


def _upload(client, path: str, source_ref: str, role: str = "inspector"):
    with Path(path).open("rb") as f:
        return client.post(
            "/api/import/records",
            headers=auth_header(role),
            files={"file": (Path(path).name, f, "text/csv")},
            data={"source_ref": source_ref},
        )


def test_health_and_auth(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.post("/api/import/records", files={}, data={"source_ref": "x"})
    assert r.status_code == 401
    r = client.get("/api/query/pending", headers=auth_header("inspector"))
    assert r.status_code == 200
    # inspector 无权批准
    r = client.post("/api/reports/1/approve", headers=auth_header("inspector"))
    assert r.status_code == 403


def test_seed_and_import_flow(client, seeded):
    r1 = _upload(client, DATA / "raw/lab_a_20261105.csv", "LAB-A")
    assert r1.status_code == 200
    body = r1.json()
    assert body["total_rows"] == 13
    assert body["pending_rows"] == 2
    assert len(body["pending_reasons"]) >= 2

    # 同文件重传：指纹识别为 duplicate
    r2 = _upload(client, DATA / "raw/lab_a_20261105.csv", "LAB-A")
    assert r2.json()["status"] == "duplicate"

    r3 = _upload(client, DATA / "raw/lab_b_20261105.json", "LAB-B")
    assert r3.status_code == 200
    assert r3.json()["conflicts_raised"] == 1


def test_broken_file_recorded_failed(client, seeded):
    r = client.post(
        "/api/import/records",
        headers=auth_header("inspector"),
        files={"file": ("bad.json", b"{broken", "application/json")},
        data={"source_ref": "SRC-BAD"},
    )
    assert r.status_code == 422
    imports = client.get("/api/query/imports", headers=auth_header("admin")).json()
    assert any(i["status"] == "failed" for i in imports)


def test_conflict_review_and_batch_report(client, seeded):
    _upload(client, DATA / "raw/lab_a_20261105.csv", "LAB-A")
    _upload(client, DATA / "raw/lab_b_20261105.json", "LAB-B")

    conflicts = client.get(
        "/api/review/conflicts?status=open", headers=auth_header("reviewer")
    ).json()
    assert len(conflicts) == 1

    # 处理 lead 冲突，采信 RB-001
    lead_conflict = next(c for c in conflicts if "RA-001" in c["reason"])
    rb001 = next(
        r for g in client.get(
            "/api/query/samples/S-001/groups", headers=auth_header("inspector")
        ).json()
        if g["analyte"] == "lead"
        for r in g["results"] if r["record_no"] == "RB-001"
    )
    resolved = client.post(
        f"/api/review/conflicts/{lead_conflict['id']}/resolve",
        headers=auth_header("reviewer"),
        json={"keep_result_id": rb001["id"], "note": "复测采信LAB-B"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"

    # 先生成批次结论，批次视图才会带 latest_conclusion
    client.post(
        "/api/reports/batch-conclusions",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261101"},
    )
    view = client.get(
        "/api/query/batches/B20261101/report", headers=auth_header("inspector")
    ).json()
    assert view["latest_conclusion"]["conclusion"] == "hold"
    sample4 = next(s for s in view["samples"] if s["sample_no"] == "S-004")
    assert sample4["status"] == "invalid"


def test_full_report_lifecycle_on_liquid_batch(client, seeded):
    _upload(client, DATA / "raw/lab_b_20261105.json", "LAB-B")

    conclusion = client.post(
        "/api/reports/batch-conclusions",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261102"},
    ).json()
    assert conclusion["conclusion"] == "pass"

    submitted = client.post(
        "/api/reports/submit",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261102", "batch_conclusion_id": conclusion["id"]},
    ).json()
    assert submitted["status"] == "submitted"

    approved = client.post(
        f"/api/reports/{submitted['id']}/approve", headers=auth_header("reviewer")
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    # 先提交第二份报告（固化当时的结果版本），随后修订到达
    second = client.post(
        "/api/reports/submit",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261102"},
    ).json()
    revised = (
        '{"records":[{"record_no":"RB-006","sample_no":"S-101","analyte":"lead",'
        '"method":"ICP-MS","reading":"8","unit":"ug/L","dilution_factor":5,'
        '"detection_limit":1,"instrument_run":"RUN-X"}]}'
    )
    client.post(
        "/api/import/records",
        headers=auth_header("inspector"),
        files={"file": ("rev.json", revised.encode(), "application/json")},
        data={"source_ref": "LAB-B"},
    )
    # 批准第二份报告时重读发现版本漂移 -> 409
    drift = client.post(
        f"/api/reports/{second['id']}/approve", headers=auth_header("reviewer")
    )
    assert drift.status_code == 409
    assert "版本漂移" in drift.json()["detail"]

    # 撤回已批准的首份报告并关联新报告，历史链可查
    withdrawn = client.post(
        f"/api/reports/{approved.json()['id']}/withdraw",
        headers=auth_header("reviewer"),
        json={"reason": "来源修订重签", "new_report_id": second["id"]},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["superseded_by_id"] == second["id"]
    reports = client.get(
        "/api/reports?batch_no=B20261102", headers=auth_header("admin")
    ).json()
    assert {r["status"] for r in reports} == {"withdrawn", "submitted"}


def test_pending_view_lists_uncertain_items(client, seeded):
    _upload(client, DATA / "raw/lab_a_20261105.csv", "LAB-A")
    pending = client.get(
        "/api/query/pending?batch_no=B20261101", headers=auth_header("inspector")
    ).json()
    # 坏行 + 总量挂起 都能看到，采购知道哪些项目还不能确定
    assert len(pending["raw_pending"]) == 2
    assert any(r["total_pending"] for r in pending["result_pending"])


def test_invalidate_sample_endpoint(client, seeded):
    r = client.post(
        "/api/import/samples/invalidate",
        headers=auth_header("reviewer"),
        json={"sample_no": "S-001", "reason": "API 标记失效测试"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "invalid"
    # inspector 无失效权限
    r2 = client.post(
        "/api/import/samples/invalidate",
        headers=auth_header("inspector"),
        json={"sample_no": "S-002", "reason": "x"},
    )
    assert r2.status_code == 403
