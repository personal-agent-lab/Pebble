"""评测器的反例校准：来源错误、结论误导和评审分歧不能通过。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.harness.checks import check_grounded
from evaluation.harness.model import TurnEvidence
from evaluation.harness.review import review_reply
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


def _judge(conclusion: str, quote: str, unsupported: list[dict] | None = None) -> str:
    return json.dumps({
        "conclusion": conclusion,
        "conclusion_quote": quote,
        "unsupported": unsupported or [],
    }, ensure_ascii=False)


def _review(reply: str, answers: list[str]) -> list:
    documents, _, _ = seed_corpus()
    by_title = {doc.title: doc.body for doc in documents}
    values = iter(answers)
    return review_reply(
        reply=reply,
        current=by_title["家庭保险"],
        old=by_title["2025 保险单据（旧）"],
        work_dir=Path("/unused"),
        ask=lambda _prompt, _path: next(values),
    )


def test_correct_current_value_and_explicit_old_value_pass():
    reply = "旧单据是 180 天，但当前《家庭保险》的等待期是 90 天。"
    answer = _judge("correct", "当前《家庭保险》的等待期是 90 天")
    assert _passed(_review(reply, [answer, answer]))


def test_negated_value_and_misleading_old_value_fail():
    for reply, quote in (
        ("等待期不是 90 天，而是 180 天。", "不是 90 天，而是 180 天"),
        ("现行等待期为 180 天，旧资料提过 90 天。", "现行等待期为 180 天"),
    ):
        answer = _judge("incorrect", quote)
        assert not _passed(_review(reply, [answer, answer]))


def test_unsupported_extra_claim_fails():
    reply = "当前等待期是 90 天，而且保费明年会翻倍。"
    answer = _judge(
        "correct", "当前等待期是 90 天",
        [{"quote": "保费明年会翻倍", "reason": "权威资料没有这个变化"}],
    )
    assert not _passed(_review(reply, [answer, answer]))


def test_review_disagreement_or_fabricated_quote_needs_manual_review():
    reply = "等待期是 90 天。"
    yes = _judge("correct", "等待期是 90 天")
    no = _judge("incorrect", "等待期是 90 天")
    for answers in ([yes, no], [_judge("correct", "回复里不存在的文字"), yes]):
        result = _review(reply, answers)
        assert not _passed(result)
        assert "待人工复核" in result[0].detail


def test_seed_fact_has_distinct_current_old_and_blank_backup():
    documents, user_md, memory_md = seed_corpus()
    by_title = {doc.title: doc.body for doc in documents}
    assert len(documents) >= 10
    assert len(user_md) <= 1375 and len(memory_md) <= 2200
    assert "| 等待期 | 90 天 |" in by_title["家庭保险"]
    assert "等待期 180 天" in by_title["2025 保险单据（旧）"]
    assert "90 天" not in by_title["家庭保险 备份"]
