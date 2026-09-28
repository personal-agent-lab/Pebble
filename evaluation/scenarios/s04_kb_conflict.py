"""S4：用户在助手读取资料后从管理页先保存，观察助手如何处理冲突。"""

from __future__ import annotations

import json
import time
from typing import Any

from ..harness.checks import check_turn
from ..harness.client import PebbleClient
from ..harness.model import Assertion, Scenario, ScenarioContext, TurnEvidence
from ..harness.review import review_task
from .common import base_seed

PATH = "kb/财务/家庭保险.md"
OLD_USER_LINE = "- 保单原件在书房抽屉，电子版在保险公司 App 里。"
NEW_USER_LINE = "- 保单原件已移到卧室书柜第二层，电子版仍在保险公司 App 里。"
MESSAGE = (
    "帮我在《家庭保险》的医疗险续保提醒后补一句：每年一月底向 HR 陈蕊确认"
    "今年的续保材料，没确认前标为待确认。其他内容别动。"
)


def _json_result(item: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(item.get("result") or "")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_old_document(item: dict[str, Any], version: str) -> bool:
    result = _json_result(item)
    return (
        item.get("kind") == "tool"
        and item.get("name") == "kb_read"
        and item.get("status") == "ok"
        and result.get("path") == PATH
        and result.get("commit") == version
        and isinstance(result.get("anchored_body"), str)
    )


def _edit_after_read(
    client: PebbleClient, task_id: str, run_id: str, *, version: str,
    original: str, timeout: float,
) -> str | None:
    """只通过产品时间线观察，再用与管理页相同的 API 保存用户改动。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = [
            item for item in client.timeline(task_id) if item.get("run_id") == run_id
        ]
        for index, item in enumerate(items):
            if not _read_old_document(item, version):
                continue
            if any(
                later.get("kind") == "tool" and later.get("name") == "kb_update"
                for later in items[index + 1 :]
            ):
                return None  # 已错过插入点；不把它误报成 agent 的冲突恢复失败。
            updated = original.replace(OLD_USER_LINE, NEW_USER_LINE, 1)
            saved = client.kb_update_document(PATH, version, updated)
            return str(saved["version"])
        run = client.get_task(task_id).get("latest_run") or {}
        if run.get("run_id") == run_id and run.get("status") in {"done", "error", "interrupted"}:
            return None
        time.sleep(0.1)
    return None


def _recovery_assertions(evidence: TurnEvidence, user_version: str) -> list[Assertion]:
    calls = evidence.invocations()
    conflicts = [
        i for i, call in enumerate(calls)
        if call.name == "kb_update" and not call.ok
        and _json_result({"result": call.result}).get("error") == "version_conflict"
    ]
    if not conflicts:
        return [Assertion("kb_conflict_recovery", False, "未观察到版本冲突，无法验证冲突后恢复")]
    conflict_index = conflicts[0]
    rereads = [
        i for i, call in enumerate(calls)
        if i > conflict_index and call.name == "kb_read" and call.ok
        and _json_result({"result": call.result}).get("commit") == user_version
    ]
    recovered = bool(rereads) and any(
        i > rereads[0] and call.name == "kb_update" and call.ok
        for i, call in enumerate(calls)
    )
    return [Assertion(
        "kb_conflict_recovery", recovered,
        "冲突后重新读取用户保存的版本并成功修改" if recovered else "冲突后未重读用户版本并完成修改",
    )]


def _run(ctx: ScenarioContext) -> list[Assertion]:
    original_doc = ctx.client.kb_document(PATH)
    original = str(original_doc["body"])
    if original.count(OLD_USER_LINE) != 1:
        raise RuntimeError("S4 种子资料缺少预期的用户修改位置")
    user_version: str | None = None

    def edit(task_id: str, run_id: str) -> None:
        nonlocal user_version
        user_version = _edit_after_read(
            ctx.client, task_id, run_id, version=str(original_doc["version"]),
            original=original, timeout=ctx.settings.turn_timeout,
        )

    evidence = ctx.converse(message=MESSAGE, on_started=edit)
    assertions = check_turn(evidence)
    assertions.append(Assertion(
        "user_saved_before_agent", user_version is not None,
        "助手读旧版后，用户通过资料管理接口先保存"
        if user_version else "未能在助手读取旧版后、修改前插入用户保存；本次未形成 S4 竞争",
    ))
    if user_version is None:
        return assertions

    final = ctx.client.kb_document(PATH)
    body = str(final["body"])
    medical_section = body.split("## 医疗险", 1)[-1].split("## ", 1)[0]
    assertions.extend([
        Assertion(
            "user_edit_preserved", NEW_USER_LINE in body and OLD_USER_LINE not in body,
            "终稿需保留用户先保存的保单位置修改",
        ),
        Assertion(
            "agent_edit_completed",
            "## 医疗险" in body
            and all(part in medical_section for part in ("一月底", "陈蕊", "续保材料", "待确认"))
            and (set(original.splitlines()) - {OLD_USER_LINE}) <= set(body.splitlines()),
            "终稿需在医疗险续保提醒中补全用户交代的要点",
        ),
    ])
    assertions.extend(_recovery_assertions(evidence, user_version))
    assertions.extend(review_task(
        task=MESSAGE,
        expected="《家庭保险》保留管理页先保存的保单位置改动，并补全医疗险续保提醒；遇版本冲突后重读再改，不覆盖用户改动。",
        reference=(
            f"用户保存前版本：{original_doc['version']}\n用户保存后版本：{user_version}"
            f"\n用户保存的内容：{NEW_USER_LINE}\n最终资料正文：\n{body}"
        ),
        evidence=[evidence],
        work_dir=ctx.data_dir / "evaluation-review",
    ))
    return assertions


SCENARIO = Scenario(
    id="S4",
    title="补写时用户先保存",
    layer="S",
    repeat=3,
    seed=base_seed,
    run=_run,
)
