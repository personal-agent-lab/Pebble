"""S4 的插入点和冲突恢复证据校验。"""

from __future__ import annotations

import json

from evaluation.harness.model import TurnEvidence
from evaluation.scenarios.s04_kb_conflict import (
    NEW_USER_LINE,
    OLD_USER_LINE,
    PATH,
    _edit_after_read,
    _recovery_assertions,
)


def _tool(name: str, status: str, result: dict, **arguments: str) -> dict:
    return {
        "kind": "tool", "run_id": "run-1", "name": name, "status": status,
        "arguments": arguments, "result": json.dumps(result, ensure_ascii=False),
    }


class FakeClient:
    def __init__(self, snapshots: list[list[dict]]) -> None:
        self.snapshots = snapshots
        self.saved: tuple[str, str, str] | None = None

    def timeline(self, _task_id: str) -> list[dict]:
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def get_task(self, _task_id: str) -> dict:
        return {"latest_run": {"run_id": "run-1", "status": "running"}}

    def kb_update_document(self, path: str, version: str, body: str) -> dict:
        self.saved = (path, version, body)
        return {"version": "user-version"}


def test_user_edit_is_inserted_after_completed_read_and_before_agent_update():
    read = _tool(
        "kb_read", "ok", {"path": PATH, "commit": "old-version", "anchored_body": "a| 原文"},
    )
    client = FakeClient([[], [read]])
    original = f"# 家庭保险\n{OLD_USER_LINE}\n"
    result = _edit_after_read(
        client, "task-1", "run-1", version="old-version", original=original, timeout=1,
    )
    assert result == "user-version"
    assert client.saved == (PATH, "old-version", original.replace(OLD_USER_LINE, NEW_USER_LINE))


def test_missed_insertion_point_is_not_counted_as_conflict_recovery():
    read = _tool(
        "kb_read", "ok", {"path": PATH, "commit": "old-version", "anchored_body": "a| 原文"},
    )
    update = _tool("kb_update", "running", {}, expected_version="old-version")
    client = FakeClient([[read, update]])
    result = _edit_after_read(
        client, "task-1", "run-1", version="old-version",
        original=OLD_USER_LINE, timeout=1,
    )
    assert result is None
    assert client.saved is None


def _evidence(calls: list[dict]) -> TurnEvidence:
    return TurnEvidence(
        task_id="task-1", run={"run_id": "run-1", "kind": "message", "status": "done"},
        timeline_items=calls, observation=None, all_observations=[], reply_text="已更新",
    )


def test_recovery_requires_conflict_then_latest_read_then_successful_update():
    conflict = _tool("kb_update", "error", {"error": "version_conflict"})
    latest = _tool("kb_read", "ok", {"path": PATH, "commit": "user-version"})
    saved = _tool("kb_update", "ok", {"version": "agent-version"})
    assert _recovery_assertions(_evidence([conflict, latest, saved]), "user-version")[0].passed
    for calls in ([latest, saved], [conflict, saved], [conflict, latest]):
        assert not _recovery_assertions(_evidence(calls), "user-version")[0].passed
