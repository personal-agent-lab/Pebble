"""S7/S8 起始状态和历史取证的边界。"""

from __future__ import annotations

import json

from evaluation.harness.model import TurnEvidence
from evaluation.harness.seeds import SeedBuilder
from evaluation.scenarios.s08_minutes_history import PATH, _history_from_previous_task
from evaluation.scenarios.s08_minutes_history import _seed as seed_minutes


def test_s8_starts_without_minutes(tmp_path):
    seed_minutes(SeedBuilder(tmp_path))
    assert not (tmp_path / PATH).exists()


def test_s8_history_evidence_requires_previous_task_and_excludes_current():
    evidence = TurnEvidence(
        task_id="current", run={"run_id": "run"}, timeline_items=[{
            "kind": "tool", "run_id": "run", "name": "history_search", "status": "ok",
            "result": json.dumps({"results": [{"task_id": "previous"}]}),
        }], observation=None, all_observations=[], reply_text="",
    )
    assert _history_from_previous_task(evidence, "previous")
    evidence.timeline_items[0]["result"] = json.dumps({
        "results": [{"task_id": "previous"}, {"task_id": "current"}],
    })
    assert not _history_from_previous_task(evidence, "previous")
