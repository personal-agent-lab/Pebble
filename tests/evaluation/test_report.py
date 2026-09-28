"""评测报告应保留完整的工具参数与返回内容。"""

from __future__ import annotations

import json

from evaluation.harness.model import TurnEvidence
from evaluation.harness.report import ScenarioRecord, write_report


def test_report_preserves_full_tool_call(tmp_path):
    long_text = "续保材料待确认" * 30
    result = {"error": "version_conflict", "current_version": "new-version"}
    evidence = TurnEvidence(
        task_id="task-1",
        run={"run_id": "run-1", "kind": "message", "status": "done"},
        timeline_items=[{
            "kind": "tool", "run_id": "run-1", "name": "kb_update", "status": "error",
            "arguments": {"operations": [{"action": "insert", "text": long_text}]},
            "result": json.dumps(result, ensure_ascii=False),
        }],
        observation=None, all_observations=[], reply_text="已重读", message="补写提醒",
    )
    record = ScenarioRecord(
        scenario_id="S4", title="补写时用户先保存", layer="S", repetition=1,
        duration_s=1, evidence=[evidence],
    )

    report = write_report(tmp_path, [record], {"started_at": "2026-09-28", "model": "auto"})
    markdown = report.read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert long_text in markdown
    assert '"current_version": "new-version"' in markdown
    assert "…" not in markdown
    assert saved["records"][0]["turns"][0]["tools"][0]["result"] == json.dumps(
        result, ensure_ascii=False,
    )
