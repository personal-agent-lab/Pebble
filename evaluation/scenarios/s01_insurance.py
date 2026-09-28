"""S1 保险笔记问答：核对现行保单的来源、回复结论与额外事实。"""

from __future__ import annotations

from ..harness.checks import check_grounded, check_turn
from ..harness.model import Assertion, Scenario, ScenarioContext
from ..harness.review import review_task
from .common import base_seed, seed_corpus


def _run(ctx: ScenarioContext) -> list[Assertion]:
    evidence = ctx.converse(message="保险那份笔记里，重疾险的等待期是多少？")
    assertions = check_turn(evidence)
    assertions.extend(
        check_grounded(evidence, path="财务/家庭保险.md", expect="90 天")
    )
    documents, _, _ = seed_corpus()
    by_title = {doc.title: doc.body for doc in documents}
    assertions.extend(
        review_task(
            task=evidence.message,
            expected=(
                "回答当前重疾险等待期为 90 天，使用资料库获取依据，不编造影响理解的保单事实。"
                "可以提及旧单据的 180 天，但应明确是旧值。"
            ),
            reference=(
                "当前生效的保单：\n" + by_title["家庭保险"]
                + "\n\n旧单据（已停用）：\n" + by_title["2025 保险单据（旧）"]
                + "\n\n备份资料：\n" + by_title["家庭保险 备份"]
            ),
            evidence=[evidence],
            work_dir=ctx.data_dir / "evaluation-review",
        )
    )
    return assertions


SCENARIO = Scenario(
    id="S1",
    title="保险笔记问答",
    layer="S",
    repeat=3,
    seed=base_seed,
    run=_run,
)
