"""评测器的反例校准：来源错误、评审失败和信息不足不能通过。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.harness.checks import check_grounded
from evaluation.harness.model import TurnEvidence
from evaluation.harness.review import review_task
from evaluation.scenarios.common import seed_corpus


def _evidence(*, tools: list[dict[str, Any]]) -> TurnEvidence:
    return TurnEvidence(
        task_id="task-1",
        run={"run_id": "run-1", "kind": "message", "status": "done"},
        timeline_items=[{"kind": "tool", "run_id": "run-1", **call} for call in tools],
        observation=None,
        all_observations=[],
        reply_text="当前等待期是 90 天。",
    )


def _call(status: str, *, path: str, body: str) -> dict[str, Any]:
    return {
        "name": "kb_read", "status": status, "arguments": {"path": path},
        "result": json.dumps({"path": path, "body": body}, ensure_ascii=False),
    }


def _passed(assertions: list) -> bool:
    return all(a.passed for a in assertions)


def test_only_successful_read_of_current_document_counts():
    path = "财务/家庭保险.md"
    fact = "| 等待期 | 90 天 |"
    assert _passed(
        check_grounded(
            _evidence(tools=[_call("ok", path=path, body=fact)]), path=path, expect=fact
        )
    )
    for calls in (
        [],
        [_call("error", path=path, body=fact)],
        [_call("ok", path="财务/其他保险.md", body=fact)],
        [_call("ok", path=path, body="没有等待期数字")],
    ):
        assert not _passed(check_grounded(_evidence(tools=calls), path=path, expect=fact))


def test_search_snippet_counts_only_when_fact_and_path_match_same_hit():
    current = "财务/家庭保险.md"
    result = {"results": [
        {"path": "kb/财务/2025 保险单据（旧）.md", "snippet": "等待期 180 天"},
        {"path": "kb/财务/家庭保险.md", "snippet": "等待期 90 天"},
    ]}
    evidence = _evidence(tools=[{
        "name": "kb_search", "status": "ok", "arguments": {"query": "重疾险 等待期"},
        "result": json.dumps(result, ensure_ascii=False),
    }])
    assert _passed(check_grounded(evidence, path=current, expect="90 天"))
    result["results"][1]["snippet"] = "等待期未填写"
    evidence = _evidence(tools=[{
        "name": "kb_search", "status": "ok", "arguments": {},
        "result": json.dumps(result, ensure_ascii=False),
    }])
    assert not _passed(check_grounded(evidence, path=current, expect="90 天"))


def _judge(completion: str = "pass", process: str = "pass") -> str:
    return json.dumps({
        "task_completion": {"status": completion, "reason": "完成情况说明"},
        "process_reasonableness": {"status": process, "reason": "过程说明"},
    }, ensure_ascii=False)


def _review(answers: list[str]) -> list:
    values = iter(answers)
    return review_task(
        task="查询邮件是否已发送", expected="如实说明尚未发送",
        reference="最终状态：草稿待确认", evidence=[_evidence(tools=[])],
        work_dir=Path("/unused"), ask=lambda _prompt, _path: next(values),
    )


def test_review_reports_completion_and_process_independently():
    for completion, process in (("pass", "pass"), ("fail", "pass"), ("pass", "fail")):
        result = _review([_judge(completion, process)])
        assert [a.passed for a in result] == [completion == "pass", process == "pass"]
        assert not any(a.pending for a in result)


def test_insufficient_evidence_preserves_other_result():
    result = _review([_judge("fail", "unclear")])
    assert not result[0].passed and not result[0].pending
    assert result[1].pending
    result = _review([_judge("unclear")])
    assert result[0].pending
    assert result[1].passed


def test_invalid_review_or_model_failure_never_passes():
    for answers in (
        ["not json"], ["[]"], ["{}"],
        [json.dumps({"task_completion": {"status": "pass", "reason": ""}})],
        [_judge("invalid")],
        [],  # 注入调用抛异常
    ):
        result = _review(answers)
        assert not _passed(result)
        assert result[0].pending


def test_review_receives_multiturn_execution_and_final_state_without_kb_dependency():
    first = _evidence(tools=[{
        "name": "calendar_create_event", "status": "error",
        "arguments": {"title": "面试"}, "result": "时间冲突",
    }])
    first.message = "安排周五面试"
    first.reply_text = "时间冲突，需要改期。"
    first.timeline_items.append({
        "kind": "tool", "run_id": "other-run", "name": "unrelated",
        "result": "不应混入",
    })
    second = _evidence(tools=[{
        "name": "calendar_create_event", "status": "ok",
        "arguments": {"title": "面试", "day": "周六"}, "result": "创建成功",
    }])
    second.message = "改到周六"
    second.reply_text = "已安排周六面试。"
    prompts = []

    def ask(prompt: str, _path: Path) -> str:
        prompts.append(json.loads(prompt))
        return _judge()

    result = review_task(
        task="安排面试，遇冲突后改期", expected="最终安排在周六",
        reference="外部查询：周六存在面试事件", evidence=[first, second],
        work_dir=Path("/unused"), ask=ask,
    )
    assert _passed(result)
    assert len(prompts) == 1
    payload = prompts[0]
    assert payload["reference"] == "外部查询：周六存在面试事件"
    assert [t["message"] for t in payload["turns"]] == ["安排周五面试", "改到周六"]
    assert payload["turns"][0]["timeline"] == first.timeline_items[:1]
    assert payload["turns"][1]["timeline"][0]["result"] == "创建成功"
    assert payload["turns"][1]["reply"] == second.reply_text


def test_seed_fact_has_distinct_current_old_and_blank_backup():
    documents, user_md, memory_md = seed_corpus()
    by_title = {doc.title: doc.body for doc in documents}
    assert len(documents) >= 10
    assert len(user_md) <= 1375 and len(memory_md) <= 2200
    assert "| 等待期 | 90 天 |" in by_title["家庭保险"]
    assert "等待期 180 天" in by_title["2025 保险单据（旧）"]
    assert "90 天" not in by_title["家庭保险 备份"]
