"""S8：先保存评审纪要，再从另一任务核实当时是否真的保存。"""

from __future__ import annotations

import json

from ..harness.checks import check_turn
from ..harness.model import Assertion, Scenario, ScenarioContext, TurnEvidence
from ..harness.review import review_task
from ..harness.seeds import SeedBuilder
from .common import seed_corpus

TITLE = "产品评审纪要 2026-09-24"
PATH = f"kb/{TITLE}.md"
SAVE = (
    "把下面这份评审纪要存进资料库，标题叫《产品评审纪要 2026-09-24》。"
    "9 月 24 日注册转化漏斗评审：老周和王总都在。已确定按“注册成功”统计，"
    "林然 9 月 25 日周五下午交竞品分析材料给老周；"
    "增长组的 A/B 实验平台采购还没定，"
    "不要给待决事项写负责人。"
)
ASK = "上次我让你存的产品评审纪要，后来真的存进资料库了吗？我当时对待决事项是怎么交代的？"


def _seed(builder: SeedBuilder) -> None:
    documents, user_md, memory_md = seed_corpus()
    # 这句具体交代只出现在任务 A 中，任务 B 应从历史找回，而非从常驻记忆猜出。
    memory_md = memory_md.replace(
        '- 会议纪要要区分"已确定"和"待决"，待决事项不写负责人。\n', ""
    )
    if "待决事项不写负责人" in memory_md:
        raise RuntimeError("S8 初始记忆仍含待从历史检索的交代")
    builder.memory(user_md, memory_md)
    builder.kb_docs([doc for doc in documents if doc.title != TITLE])


def _history_from_previous_task(evidence: TurnEvidence, previous_id: str) -> bool:
    for call in evidence.invocations():
        if call.name != "history_search" or not call.ok:
            continue
        try:
            result = json.loads(call.result)
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        hits = result.get("results")
        if not isinstance(hits, list):
            continue
        ids = {hit.get("task_id") for hit in hits if isinstance(hit, dict)}
        if previous_id in ids and evidence.task_id not in ids:
            return True
    return False


def _run(ctx: ScenarioContext) -> list[Assertion]:
    first = ctx.converse(message=SAVE)
    listing = ctx.client.kb_documents()
    matches = [
        doc for doc in listing.get("documents", [])
        if isinstance(doc, dict) and doc.get("title") == TITLE
    ]
    saved_path = str(matches[0]["path"]) if len(matches) == 1 else None
    saved = ctx.client.kb_document(saved_path) if saved_path else None
    second = ctx.converse(message=ASK)
    assertions = [item for turn in (first, second) for item in check_turn(turn)]
    assertions.extend([
        Assertion(
            "minutes_saved",
            saved is not None and all(
                phrase in str(saved.get("body") or "")
                for phrase in ("注册成功", "周五", "下午", "A/B", "待决")
            ) and not any(
                wrong in str(saved.get("body") or "")
                for wrong in ("9 月 26", "9月26", "09-26", "09/26")
            ),
            "首个任务应把关键结论和待决事项实际存进资料库",
        ),
        Assertion(
            "history_excludes_current_task",
            _history_from_previous_task(second, first.task_id),
            "后续任务的历史检索应命中先前任务，且不返回当前任务",
        ),
    ])
    assertions.extend(review_task(
        task=f"先执行：{SAVE}\n另开任务询问：{ASK}",
        expected=(
            "第一任务确实保存纪要，保留已确定与待决事项的区别；"
            "第二任务核对过去的对话及资料库终态，如实回答是否保存，"
            "并说清用户当时交代待决事项不要写负责人。"
        ),
        reference=f"实际资料库路径：{saved_path}\n最终资料：{saved}",
        evidence=[first, second],
        work_dir=ctx.data_dir / "evaluation-review",
    ))
    return assertions


SCENARIO = Scenario(
    id="S8", title="纪要存了吗", layer="S", repeat=3, seed=_seed, run=_run,
)
