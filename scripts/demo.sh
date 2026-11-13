#!/bin/bash
# 端到端演示：导入 -> 去重 -> 冲突复核 -> 批次结论 -> 报告批准/待处理视图。
# 前置：服务已在 http://127.0.0.1:8000 运行，参考数据已 seed。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
IH="Authorization: Bearer token-inspector-001"
RH="Authorization: Bearer token-reviewer-001"
py() { python3 -c "import sys,json;x=json.load(sys.stdin);$1"; }

echo "== 1. 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 2. 导入 LAB-A CSV（坏行进入待处理，不丢行） =="
curl -s -X POST "$BASE/api/import/records" -H "$IH" \
  -F "source_ref=LAB-A" -F "file=@data/raw/lab_a_20261105.csv" \
  | py "print('status=',x['status'],'total=',x['total_rows'],'parsed=',x['parsed_rows'],'pending=',x['pending_rows'])"

echo "== 3. 同文件重传（来源编号+指纹识别为重复，跳过） =="
curl -s -X POST "$BASE/api/import/records" -H "$IH" \
  -F "source_ref=LAB-A" -F "file=@data/raw/lab_a_20261105.csv" \
  | py "print('status=',x['status'],'note=',x['note'])"

echo "== 4. 导入 LAB-B JSON（异源结果不一致 -> 列冲突，不覆盖） =="
curl -s -X POST "$BASE/api/import/records" -H "$IH" \
  -F "source_ref=LAB-B" -F "file=@data/raw/lab_b_20261105.json" \
  | py "print('conflicts_raised=',x['conflicts_raised'])"

echo "== 5. 复核员查看并处理冲突（采信 LAB-B 的 0.35 mg/kg 高值） =="
CID=$(curl -s "$BASE/api/review/conflicts?status=open" -H "$RH" \
  | py "print(x[0]['id'])")
KEEP=$(curl -s "$BASE/api/query/samples/S-001/groups" -H "$IH" | py "
g=[g for g in x if g['analyte']=='lead'][0]
print([r['id'] for r in g['results'] if r['record_no']=='RB-001'][0])")
echo "conflict_id=$CID, keep result_id=$KEEP"
curl -s -X POST "$BASE/api/review/conflicts/$CID/resolve" -H "$RH" -H "Content-Type: application/json" \
  -d "{\"keep_result_id\":$KEEP,\"note\":\"演示：复测采信LAB-B高值\"}" \
  | py "print('conflict status=',x['status'])"

echo "== 6. 生成批次结论（固体 B20261101=hold，液体 B20261102=pass） =="
curl -s -X POST "$BASE/api/reports/batch-conclusions" -H "$IH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261101"}' \
  | py "print('B20261101 =>',x['conclusion'],'| 阻断数=',len(x['detail']['blockers']))"
LCID=$(curl -s -X POST "$BASE/api/reports/batch-conclusions" -H "$IH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261102"}' | py "print(x['id'])")

echo "== 7. 提交并批准液体批次报告 =="
RID=$(curl -s -X POST "$BASE/api/reports/submit" -H "$IH" -H "Content-Type: application/json" \
  -d "{\"batch_no\":\"B20261102\",\"batch_conclusion_id\":$LCID}" | py "print(x['id'])")
curl -s -X POST "$BASE/api/reports/$RID/approve" -H "$RH" \
  | py "print('report',x['id'],'=>',x['status'])"

echo "== 8. 待处理视图（采购可见的不确定项） =="
curl -s "$BASE/api/query/pending?batch_no=B20261101" -H "$IH" \
  | py "print('raw_pending(坏行)=',len(x['raw_pending']),'result_pending(待补/总量挂起)=',len(x['result_pending']))"

echo "== 9. 批次报告视图中的关键判定 =="
curl -s "$BASE/api/query/batches/B20261101/report" -H "$IH" | py "
c=x['latest_conclusion']
print('结论=',c['conclusion'])
for it in c['detail']['items']:
    if it['analyte'] in ('lead','mercury') and it['sample_no'] in ('S-001','S-002'):
        print(' ',it['sample_no'],it['analyte'],it['verdict'],it['interval'],it['limit'])"

echo
echo "== 10. 质控监测：登记样品运行的真实顺序 =="
curl -s -X POST "$BASE/api/qc/runs" -H "$IH" -H "Content-Type: application/json" \
  -d '{"run_id":"RUN-1101","instrument":"ICPMS-01","run_seq":103,"note":"ICP-MS 样品运行"}' \
  | py "print('run=',x['run_id'],'instrument=',x['instrument'],'seq=',x['run_seq'])"

echo "== 11. 提交固体批次报告 R1（稍后被质控影响暂缓批准） =="
R1=$(curl -s -X POST "$BASE/api/reports/submit" -H "$IH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261101"}' | py "print(x['id'])")
echo "report R1 id=$R1"

echo "== 12. 录入质控观测主序列（seq 103 的记录迟到，稍后补） =="
curl -s -X POST "$BASE/api/qc/observations" -H "$IH" -H "Content-Type: application/json" \
  -d @data/raw/qc_observations_20261106.json \
  | py "print('inserted=',x['inserted'],'errors=',len(x['errors']),'evaluations=',x['evaluations'])"

echo "== 13. 监测结论：趋势=疑似漂移，越界=已证实失效（分开告警） =="
curl -s "$BASE/api/qc/alerts?status=open" -H "$RH" | py "
for a in x:
    t=a['trigger_data']
    print(f\"  告警#{a['id']} {a['alert_type']}({t['verdict']}) run={t['run_id']} 观测={t['observed_value']} 偏差={t['deviation']:.3f} 连续同向={t['consecutive_same_direction']}\")"

echo "== 14. 影响追查：最近可靠质控后的运行 -> 样品 -> 报告；缺映射列无法定位 =="
curl -s "$BASE/api/qc/trace-runs" -H "$RH" | py "
t=[r for r in x if r['alert_seq']==106][0]
print('  可靠点 seq=',t['reliable_seq'],'窗口运行=',[r['run_id'] for r in t['window_runs']])
print('  受影响报告=',t['reports_added'],'无法定位运行=',[u['run_id'] for u in t['unlocatable']])"

echo "== 15. R1 批准被暂缓（409，逐项列出触发数据与解除依据） =="
curl -s -X POST "$BASE/api/reports/$R1/approve" -H "$RH" | py "print(' ',x['detail'][:160],'...')"

echo "== 16. 主管逐项登记校准/复测证据解除（两次限制须全部解除） =="
HOLDS=$(curl -s "$BASE/api/qc/reports/$R1/holds" -H "$RH")
IMP1=$(echo "$HOLDS" | py "print(x['open_impacts'][0]['id'])")
IMP2=$(echo "$HOLDS" | py "print(x['open_impacts'][1]['id'])")
curl -s -X POST "$BASE/api/qc/impacts/$IMP1/clear" -H "$RH" -H "Content-Type: application/json" \
  -d '{"evidence_type":"recalibration","evidence_ref":"CAL-20261107-ICPMS01","note":"重新校准后质控回控"}' \
  | py "print('  影响#',x['id'],'=>',x['status'],'证据=',x['clearance_evidence_ref'])"
curl -s -X POST "$BASE/api/reports/$R1/approve" -H "$RH" | py "print('  仅解除一项后批准 =>',x.get('detail','OK')[:80],'...')"
curl -s -X POST "$BASE/api/qc/impacts/$IMP2/clear" -H "$RH" -H "Content-Type: application/json" \
  -d '{"evidence_type":"retest","evidence_ref":"RETEST-20261107-01","note":"受影响运行样品复测一致"}' \
  | py "print('  影响#',x['id'],'=>',x['status'],'证据=',x['clearance_evidence_ref'])"
curl -s -X POST "$BASE/api/reports/$R1/approve" -H "$RH" \
  | py "print('  全部解除后 R1 =>',x['status'])"

echo "== 17. 同批次新报告 R2 提交即挂上未结影响 =="
R2=$(curl -s -X POST "$BASE/api/reports/submit" -H "$IH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261101"}' | py "print(x['id'])")
curl -s "$BASE/api/qc/reports/$R2/holds" -H "$RH" \
  | py "print('  R2 id=$R2 未结影响=',len(x['open_impacts']),'项')"

echo "== 18. 迟到记录插回原顺序重算：趋势被打断、可靠点后移 =="
curl -s -X POST "$BASE/api/qc/observations" -H "$IH" -H "Content-Type: application/json" \
  -d @data/raw/qc_observations_20261106_late.json \
  | py "print('  inserted=',x['inserted'],'evaluations=',x['evaluations'])"
curl -s "$BASE/api/qc/alerts" -H "$RH" | py "
for a in x:
    print(f\"  告警#{a['id']} {a['alert_type']} v{a['config_version']} => {a['status']}\")"

echo "== 19. 影响通知只增不删：R2 移出补发 removed，历史全部保留 =="
curl -s "$BASE/api/qc/notifications" -H "$RH" | py "
for n in x:
    print(f\"  通知#{n['id']} {n['action']} 报告={n['report_no']} 告警#{n['alert_id']}\")"
curl -s -X POST "$BASE/api/reports/$R2/approve" -H "$RH" \
  | py "print('  重算移出后 R2 =>',x['status'])"

echo
echo "演示完成。Swagger UI: $BASE/docs"
