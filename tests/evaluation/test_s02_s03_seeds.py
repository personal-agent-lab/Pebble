"""新场景的初始状态必须让用户动作真正产生变化。"""

from __future__ import annotations

import json

from evaluation.harness.model import TurnEvidence
from evaluation.harness.seeds import SeedBuilder
from evaluation.scenarios.s02_memory import _seed as seed_memory
from evaluation.scenarios.s03_kb_intake import TRIP, _agent_found_document, _retitle
from evaluation.scenarios.s03_kb_intake import _seed as seed_kb


def test_s2_starts_without_facts_it_asks_agent_to_learn(tmp_path):
    seed_memory(SeedBuilder(tmp_path))
    text = "\n".join(
        (tmp_path / "memory" / name).read_text(encoding="utf-8")
        for name in ("USER.md", "MEMORY.md")
    )
    assert "11 月 8" not in text
    assert "周二上午固定" not in text


def test_s3_starts_before_editor_changes(tmp_path):
    seed_kb(SeedBuilder(tmp_path))
    kb = tmp_path / "kb"
    assert (kb / "财务" / "保险单据.md").exists()
    assert (kb / "财务" / "2025 房租合同.md").exists()
    assert not (kb / "财务" / "家庭保险.md").exists()
    assert not (kb / "财务" / "家庭保险 备份.md").exists()
    assert not (kb / "2026 国庆出行计划.md").exists()


def test_direct_rename_keeps_frontmatter_and_heading_consistent():
    raw = "---\nid: kb-1\ntitle: 保险单据\n---\n\n# 保险单据\n正文\n"
    updated = _retitle(raw, "保险单据", "家庭保险")
    assert "id: kb-1" in updated
    assert "title: 家庭保险" in updated
    assert "# 家庭保险" in updated


def test_s3_accepts_current_document_from_list_or_search():
    for name, field in (("kb_list", "documents"), ("kb_search", "results")):
        evidence = TurnEvidence(
            task_id="task", run={"run_id": "run"}, observation=None,
            all_observations=[], reply_text="", timeline_items=[{
                "kind": "tool", "run_id": "run", "name": name, "status": "ok",
                "result": json.dumps({field: [{"path": TRIP}]}),
            }],
        )
        assert _agent_found_document(evidence)
        evidence.timeline_items[0]["result"] = json.dumps({field: [{"path": "kb/other.md"}]})
        assert not _agent_found_document(evidence)
