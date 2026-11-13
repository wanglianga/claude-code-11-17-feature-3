"""质控样漂移监测与受影响报告追查测试。

覆盖需求点：
- 标准值/观察范围按组保存，配置版本化，修改不擦旧告警
- 按真实运行顺序计算偏差/连续同向/滑动均值；校准变更切段不混算
- 越界=已证实失效、趋势=疑似漂移，两类告警区分
- 影响追查：最近可靠质控后的运行 -> 样品 -> 报告；缺映射列无法定位
- 报告批准暂缓，主管登记校准/复测证据逐项解除；全部解除才恢复
- 迟到记录插回原顺序重算，通知保留并说明报告增减
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import auth_header

DATA = Path(__file__).resolve().parents[1] / "data"

LEAD_GROUP = {
    "qc_material": "QC-PB-CRM01",
    "analyte": "lead",
    "method": "ICP-MS",
    "instrument": "ICPMS-01",
}


# ---- 辅助 -------------------------------------------------------------------

def _upload_records(client, path, source_ref, role="inspector"):
    with Path(path).open("rb") as f:
        return client.post(
            "/api/import/records",
            headers=auth_header(role),
            files={"file": (Path(path).name, f, "text/csv")},
            data={"source_ref": source_ref},
        )


def _register_run(client, run_id, instrument, run_seq, role="inspector"):
    return client.post(
        "/api/qc/runs",
        headers=auth_header(role),
        json={"run_id": run_id, "instrument": instrument, "run_seq": run_seq},
    )


def _feed_qc_file(client, path, role="inspector"):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return client.post(
        "/api/qc/observations",
        headers=auth_header(role),
        json={"observations": payload["observations"]},
    )


def _obs(client, run_id, run_seq, value, role="inspector", **overrides):
    item = {
        **LEAD_GROUP,
        "observed_value": value,
        "unit": "mg/kg",
        "run_id": run_id,
        "run_seq": run_seq,
    }
    item.update(overrides)
    return client.post(
        "/api/qc/observations", headers=auth_header(role),
        json={"observations": [item]},
    )


def _prepare_report(client):
    """导入 LAB-A、登记样品运行顺序、生成结论并提交 B20261101 报告。"""
    _upload_records(client, DATA / "raw/lab_a_20261105.csv", "LAB-A")
    _register_run(client, "RUN-1101", "ICPMS-01", 103)
    conclusion = client.post(
        "/api/reports/batch-conclusions",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261101"},
    ).json()
    return client.post(
        "/api/reports/submit",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261101", "batch_conclusion_id": conclusion["id"]},
    ).json()


def _feed_main_sequence(client):
    """主质控序列：101:0.51 / 102:0.53 / 104:0.55 / 105:0.57 / 106:0.65。"""
    return _feed_qc_file(client, DATA / "raw/qc_observations_20261106.json")


def _alerts(client, **filters):
    qs = "&".join(f"{k}={v}" for k, v in filters.items())
    return client.get(f"/api/qc/alerts?{qs}", headers=auth_header("reviewer")).json()


# ---- 配置管理 -----------------------------------------------------------------

def test_seeded_reference_and_versioned_upsert(client, seeded):
    refs = client.get(
        "/api/qc/references?status=active", headers=auth_header("inspector")
    ).json()
    assert len(refs) == 2
    lead = next(r for r in refs if r["analyte"] == "lead")
    assert lead["assigned_value"] == 0.5
    assert lead["range_low"] == 0.4 and lead["range_high"] == 0.6
    assert lead["version"] == 1

    # 同参数重复提交：幂等，不产生新版本
    same = {k: lead[k] for k in (
        "qc_material", "analyte", "method", "instrument", "assigned_value",
        "range_low", "range_high", "unit", "window_size", "run_limit",
        "mean_deviation_limit",
    )}
    r = client.post("/api/qc/references", headers=auth_header("admin"), json=same)
    assert r.status_code == 201
    assert r.json()["version"] == 1

    # 参数变化：新版本生效，旧版本 retired 保留
    changed = {**same, "range_high": 0.65}
    r = client.post("/api/qc/references", headers=auth_header("admin"), json=changed)
    assert r.status_code == 201
    assert r.json()["version"] == 2
    all_refs = client.get("/api/qc/references", headers=auth_header("admin")).json()
    lead_refs = [x for x in all_refs if x["analyte"] == "lead"]
    assert {(x["version"], x["status"]) for x in lead_refs} == {
        (1, "retired"), (2, "active"),
    }


def test_reference_validation(client, seeded):
    base = client.get(
        "/api/qc/references?status=active", headers=auth_header("admin")
    ).json()[0]
    bad = {k: base[k] for k in (
        "qc_material", "analyte", "method", "instrument", "assigned_value",
        "range_low", "range_high", "unit", "window_size", "run_limit",
        "mean_deviation_limit",
    )}
    bad["range_low"], bad["range_high"] = 0.9, 0.1
    r = client.post("/api/qc/references", headers=auth_header("admin"), json=bad)
    assert r.status_code == 400


# ---- 监测计算 -----------------------------------------------------------------

def test_out_of_range_is_confirmed_failure(client, seeded):
    r = _obs(client, "RUN-X1", 1, 0.65)
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    alerts = _alerts(client, alert_type="out_of_range")
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["alert_type"] == "out_of_range"
    assert alert["trigger_data"]["verdict"] == "confirmed_failure"
    assert alert["trigger_data"]["observed_value"] == 0.65
    assert alert["trigger_data"]["deviation"] == 0.15000000000000002 or \
        abs(alert["trigger_data"]["deviation"] - 0.15) < 1e-9
    assert alert["trigger_data"]["approved_range"] == [0.4, 0.6]


def test_consecutive_same_direction_drift_without_violation(client, seeded):
    # 全部在观察范围内，但连续 3 次同向偏移 -> 疑似漂移，不是已证实失效
    for seq, value in ((1, 0.52), (2, 0.54), (3, 0.56)):
        _obs(client, f"RUN-D{seq}", seq, value)
    drift = _alerts(client, alert_type="drift_trend")
    assert len(drift) == 1
    assert drift[0]["trigger_data"]["verdict"] == "suspected_drift"
    assert drift[0]["trigger_data"]["consecutive_same_direction"] == 3
    # 没有越界告警：疑似漂移不等于已证实失效
    assert _alerts(client, alert_type="out_of_range") == []


def test_moving_average_drift(client, seeded):
    # 自定义 run_limit=5：连续同向 3 次不够，但滑动均值偏差达阈值 -> 仍应预警
    cfg = {
        "qc_material": "QC-CD-CRM03", "analyte": "cadmium", "method": "ICP-MS",
        "instrument": "ICPMS-09", "assigned_value": 0.3, "range_low": 0.2,
        "range_high": 0.4, "unit": "mg/kg", "window_size": 3, "run_limit": 5,
        "mean_deviation_limit": 0.05,
    }
    assert client.post(
        "/api/qc/references", headers=auth_header("admin"), json=cfg
    ).status_code == 201
    for seq, value in ((1, 0.34), (2, 0.36), (3, 0.35)):
        _obs(client, f"RUN-M{seq}", seq, value, instrument="ICPMS-09",
             qc_material="QC-CD-CRM03", analyte="cadmium")
    drift = _alerts(client, alert_type="drift_trend", instrument="ICPMS-09")
    assert len(drift) == 1
    td = drift[0]["trigger_data"]
    assert td["consecutive_same_direction"] == 3 < td["run_limit"]
    assert abs(td["moving_avg_deviation"] - 0.05) < 1e-9


def test_calibration_segments_not_mixed(client, seeded):
    # 校准变更切段：跨段的连续同向不能混算成一次趋势
    client.post(
        "/api/qc/calibrations", headers=auth_header("admin"),
        json={"instrument": "ICPMS-01", "effective_seq": 110,
              "note": "更换雾化器后重新校准"},
    )
    # 段1：seq 108/109 正向 2 次；段2：seq 110/111 正向 2 次 —— 混算会误判 4 连
    for seq, value in ((108, 0.53), (109, 0.54), (110, 0.55), (111, 0.56)):
        _obs(client, f"RUN-C{seq}", seq, value)
    assert _alerts(client, alert_type="drift_trend") == []
    # 段内再续一次 -> 段内连续 3 次，独立触发
    _obs(client, "RUN-C112", 112, 0.57)
    drift = _alerts(client, alert_type="drift_trend")
    assert len(drift) == 1
    assert drift[0]["segment_start"] == 110
    assert drift[0]["trigger_data"]["consecutive_same_direction"] == 3


def test_evaluation_detail_snapshot(client, seeded):
    _feed_main_sequence(client)
    runs = client.get("/api/qc/monitor/runs", headers=auth_header("reviewer")).json()
    assert len(runs) == 1
    detail = runs[0]["detail"]
    by_seq = {d["run_seq"]: d for d in detail}
    assert by_seq[101]["consecutive_same_direction"] == 1
    assert by_seq[104]["consecutive_same_direction"] == 3
    assert by_seq[104]["reliable"] is False   # 趋势触发点不再是可靠质控
    assert by_seq[102]["reliable"] is True
    assert by_seq[106]["out_of_range"] is True


# ---- 影响追查与批准暂缓 ---------------------------------------------------------

def test_impact_trace_holds_report_and_unlocatable(client, seeded):
    report = _prepare_report(client)
    _feed_main_sequence(client)

    alerts = _alerts(client, status="open")
    assert {a["alert_type"] for a in alerts} == {"drift_trend", "out_of_range"}

    # 报告的未结限制：两条告警各产生一项，含触发数据与解除依据说明
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    assert len(holds["open_impacts"]) == 2
    summary = holds["open_impacts"][0]["trigger_summary"]
    assert summary["trigger_data"]["observed_value"] is not None
    assert summary["reliable_seq"] == 102

    # 批准被暂缓，409 中给出每项限制的触发数据
    r = client.post(
        f"/api/reports/{report['id']}/approve", headers=auth_header("reviewer")
    )
    assert r.status_code == 409
    assert "限制#" in r.json()["detail"]
    assert "解除依据" in r.json()["detail"]

    # 追查留痕：窗口内运行、受影响报告、无法定位运行
    traces = client.get("/api/qc/trace-runs", headers=auth_header("reviewer")).json()
    oor_trace = next(t for t in traces if t["alert_seq"] == 106)
    assert {r["run_id"] for r in oor_trace["window_runs"]} == {
        "RUN-1101", "RUN-Q204", "RUN-Q205", "RUN-Q206",
    }
    assert {u["run_id"] for u in oor_trace["unlocatable"]} == {
        "RUN-Q204", "RUN-Q205", "RUN-Q206",
    }
    assert any(a["run_id"] == "RUN-1101" and report["report_no"] in a["report_nos"]
               for a in oor_trace["affected"])
    assert report["report_no"] in oor_trace["reports_added"]


def test_clear_impacts_one_by_one_then_approve(client, seeded):
    report = _prepare_report(client)
    _feed_main_sequence(client)
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    impacts = holds["open_impacts"]
    assert len(impacts) == 2

    # 解除第一项：报告仍被第二项限制（两次复核涉及同一报告，须全部解除）
    r = client.post(
        f"/api/qc/impacts/{impacts[0]['id']}/clear",
        headers=auth_header("reviewer"),
        json={"evidence_type": "recalibration",
              "evidence_ref": "CAL-20261107-ICPMS01",
              "note": "重新校准后质控回控"},
    )
    assert r.status_code == 200
    assert r.json()["clearance_evidence_type"] == "recalibration"
    r = client.post(
        f"/api/reports/{report['id']}/approve", headers=auth_header("reviewer")
    )
    assert r.status_code == 409

    # 全部解除后恢复批准
    r = client.post(
        f"/api/qc/impacts/{impacts[1]['id']}/clear",
        headers=auth_header("reviewer"),
        json={"evidence_type": "retest",
              "evidence_ref": "RETEST-20261107-01",
              "note": "受影响运行样品复测结果一致"},
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/reports/{report['id']}/approve", headers=auth_header("reviewer")
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    # 解除依据留痕可查
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    assert len(holds["open_impacts"]) == 0
    assert {i["clearance_evidence_type"] for i in holds["cleared_impacts"]} == {
        "recalibration", "retest",
    }


def test_alert_resolve_does_not_release_report(client, seeded):
    report = _prepare_report(client)
    _feed_main_sequence(client)
    for alert in _alerts(client, status="open"):
        r = client.post(
            f"/api/qc/alerts/{alert['id']}/resolve",
            headers=auth_header("reviewer"),
            json={"note": "主管已知悉，安排校准"},
        )
        assert r.status_code == 200
    # 单个告警关闭不能越过影响限制：报告仍被暂缓
    r = client.post(
        f"/api/reports/{report['id']}/approve", headers=auth_header("reviewer")
    )
    assert r.status_code == 409
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    assert len(holds["open_impacts"]) == 2


def test_new_report_inherits_impact_on_submit(client, seeded):
    # 先出告警，后提交报告：新报告提交即挂上影响
    _upload_records(client, DATA / "raw/lab_a_20261105.csv", "LAB-A")
    _register_run(client, "RUN-1101", "ICPMS-01", 103)
    _feed_main_sequence(client)
    conclusion = client.post(
        "/api/reports/batch-conclusions",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261101"},
    ).json()
    report = client.post(
        "/api/reports/submit",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261101", "batch_conclusion_id": conclusion["id"]},
    ).json()
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    assert len(holds["open_impacts"]) == 2


def test_unknown_order_run_is_unlocatable_not_clean(client, seeded):
    _upload_records(client, DATA / "raw/lab_a_20261105.csv", "LAB-A")
    # 样品运行登记了仪器但顺序未知
    _register_run(client, "RUN-1101", "ICPMS-01", None)
    conclusion = client.post(
        "/api/reports/batch-conclusions",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261101"},
    ).json()
    report = client.post(
        "/api/reports/submit",
        headers=auth_header("inspector"),
        json={"batch_no": "B20261101", "batch_conclusion_id": conclusion["id"]},
    ).json()
    _feed_main_sequence(client)
    # 顺序未知不能排除在窗口外：保守纳入影响（标注 order_unknown）
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    assert len(holds["open_impacts"]) == 2
    traces = client.get("/api/qc/trace-runs", headers=auth_header("reviewer")).json()
    oor_trace = next(t for t in traces if t["alert_seq"] == 106)
    run1101 = next(a for a in oor_trace["affected"] if a["run_id"] == "RUN-1101")
    assert run1101["order_unknown"] is True


# ---- 迟到记录重算 ---------------------------------------------------------------

def test_late_observation_recomputes_and_keeps_notifications(client, seeded):
    report = _prepare_report(client)
    _feed_main_sequence(client)
    before = client.get(
        "/api/qc/notifications", headers=auth_header("reviewer")
    ).json()
    assert len([n for n in before if n["action"] == "added"]) == 2

    # 迟到记录 RUN-Q203(seq 103)=0.44 插回原顺序：趋势被打断，可靠点后移
    r = _feed_qc_file(client, DATA / "raw/qc_observations_20261106_late.json")
    assert r.json()["inserted"] == 1

    # 原 RUN-Q204 趋势告警被重算取代（保留历史）；越界告警仍成立
    drift = _alerts(client, alert_type="drift_trend")
    assert {(a["status"]) for a in drift} >= {"open", "superseded"}
    superseded = [a for a in drift if a["status"] == "superseded"]
    assert len(superseded) == 1
    assert superseded[0]["trigger_data"]["run_id"] == "RUN-Q204"
    oor = _alerts(client, alert_type="out_of_range", status="open")
    assert len(oor) == 1

    # 影响通知：已发出的 added 保留，重算后报告移出补发 removed
    notes = client.get("/api/qc/notifications", headers=auth_header("reviewer")).json()
    added = [n for n in notes if n["action"] == "added"]
    removed = [n for n in notes if n["action"] == "removed"]
    assert len(added) == 2 and len(removed) == 2
    assert all(n["report_no"] == report["report_no"] for n in removed)

    # 影响全部自动移出（auto_recompute），报告恢复可批准
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    assert holds["open_impacts"] == []
    assert {i["clearance_evidence_type"] for i in holds["cleared_impacts"]} == {
        "auto_recompute"
    }
    r = client.post(
        f"/api/reports/{report['id']}/approve", headers=auth_header("reviewer")
    )
    assert r.status_code == 200


def test_duplicate_observation_idempotent(client, seeded):
    _feed_main_sequence(client)
    r = _feed_main_sequence(client)  # 同文件重传
    assert r.json()["inserted"] == 0
    assert len(r.json()["skipped"]) == 5
    # 不重复产生告警
    assert len(_alerts(client, status="open")) == 2


def test_manual_clearance_not_reopened_by_retrace(client, seeded):
    """人工登记证据解除后，同一告警的重算/再追查不得推翻人工结论。"""
    report = _prepare_report(client)
    _feed_main_sequence(client)
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    for impact in holds["open_impacts"]:
        client.post(
            f"/api/qc/impacts/{impact['id']}/clear",
            headers=auth_header("reviewer"),
            json={"evidence_type": "recalibration",
                  "evidence_ref": "CAL-20261107-01", "note": "校准后回控"},
        )
    # 触发再追查（手动 monitor run 会对全部 open 告警重算影响）
    r = client.post("/api/qc/monitor/run", headers=auth_header("reviewer"), json={})
    assert r.status_code == 200
    holds = client.get(
        f"/api/qc/reports/{report['id']}/holds", headers=auth_header("reviewer")
    ).json()
    assert holds["open_impacts"] == []  # 人工解除不被重算推翻
    assert all(i["clearance_evidence_type"] == "recalibration"
               for i in holds["cleared_impacts"])
    # 通知不重复 added
    notes = client.get(
        f"/api/qc/notifications?report_id={report['id']}",
        headers=auth_header("reviewer"),
    ).json()
    assert [n["action"] for n in notes] == ["added", "added"]


# ---- 配置修改不擦旧告警 -----------------------------------------------------------

def test_config_change_keeps_old_alerts(client, seeded):
    _prepare_report(client)
    _feed_main_sequence(client)
    old_alerts = _alerts(client, status="open")
    assert len(old_alerts) == 2

    # 放宽观察范围（0.65 不再越界）：另算结果，但旧告警不能被擦掉
    new_cfg = {
        **LEAD_GROUP, "assigned_value": 0.5, "range_low": 0.4, "range_high": 0.7,
        "unit": "mg/kg", "window_size": 3, "run_limit": 3,
        "mean_deviation_limit": 0.05, "note": "校准后重新批准的观察范围",
    }
    r = client.post("/api/qc/references", headers=auth_header("admin"), json=new_cfg)
    assert r.json()["version"] == 2

    all_alerts = _alerts(client)
    v1_alerts = [a for a in all_alerts if a["config_version"] == 1]
    assert len(v1_alerts) == 2
    assert all(a["status"] == "open" for a in v1_alerts)  # 旧告警保留未擦除
    # 新配置下越界告警不再产生，趋势告警按新配置另算
    v2_oor = [a for a in all_alerts
              if a["config_version"] == 2 and a["alert_type"] == "out_of_range"]
    assert v2_oor == []
    # 旧告警的影响仍在，报告批准仍受限
    holds = client.get(
        "/api/qc/reports/{}/holds".format(
            client.get("/api/reports", headers=auth_header("reviewer"),
                       ).json()[0]["id"]),
        headers=auth_header("reviewer"),
    ).json()
    assert len(holds["open_impacts"]) >= 2


# ---- 观测录入校验 -----------------------------------------------------------------

def test_observation_validation(client, seeded):
    # 无配置的质控组
    r = _obs(client, "RUN-Z1", 1, 0.5, instrument="UNKNOWN-INS")
    assert r.json()["inserted"] == 0
    assert "配置" in r.json()["errors"][0]["reason"]
    # 单位与配置不一致
    r = _obs(client, "RUN-Z2", 1, 0.5, unit="ug/kg")
    assert r.json()["inserted"] == 0
    assert "单位" in r.json()["errors"][0]["reason"]
    # 运行顺序冲突：同一 run_id 登记了不同顺序
    _obs(client, "RUN-Z3", 10, 0.5)
    r = client.post(
        "/api/qc/runs", headers=auth_header("inspector"),
        json={"run_id": "RUN-Z3", "instrument": "ICPMS-01", "run_seq": 99},
    )
    assert r.status_code == 409


def test_qc_permissions(client, seeded):
    cfg = {
        **LEAD_GROUP, "assigned_value": 0.5, "range_low": 0.4, "range_high": 0.6,
        "unit": "mg/kg", "window_size": 3, "run_limit": 3,
        "mean_deviation_limit": 0.05,
    }
    # inspector 不能改配置，reviewer 不能录入观测
    assert client.post(
        "/api/qc/references", headers=auth_header("inspector"), json=cfg
    ).status_code == 403
    assert client.post(
        "/api/qc/calibrations", headers=auth_header("inspector"),
        json={"instrument": "ICPMS-01", "effective_seq": 1},
    ).status_code == 403
    r = _obs(client, "RUN-P1", 1, 0.5, role="reviewer")
    assert r.status_code == 403
    # inspector 可以录入观测与运行监测
    assert _obs(client, "RUN-P2", 2, 0.5).status_code == 200
    assert client.post(
        "/api/qc/monitor/run", headers=auth_header("inspector"), json={}
    ).status_code == 200
    # inspector 不能解除影响
    assert client.post(
        "/api/qc/impacts/1/clear", headers=auth_header("inspector"),
        json={"evidence_type": "retest", "evidence_ref": "X", "note": "Y"},
    ).status_code == 403
