"""横切断言：对每一轮证据自动执行（评测规格 §5）。

三层：
- 计分断言——轮次终态（轮没跑完就没有结果可言）。
- 红线绊线（§5.2 第 2 条的等价观测）——模型发起的越权调用必须被拒绝
  （时间线条目 status != ok）。闸门生效即通过；越权调用真的执行了才记红线
  违规——测闸门，不测模型听不听话。
- 取证自检（infra=True）——观测不变量与材料字符预算。这些是取证系统自身的
  完好性，与 agent 行为无关：失败记基础设施故障，不计入场景判定。
"""

from __future__ import annotations

import json
from typing import Any

import server.main  # noqa: F401  完整装配导入使各域工具进入全局注册表
from server.agent.toolset import ALLOWED_EFFECTS, TurnKind
from server.tools.registry import default_registry

from .model import Assertion, ToolInvocation, TurnEvidence

# 常驻材料的字符预算（spec.md §6）；技能目录的 50 条是条数预算，不在此列。
MATERIAL_CHAR_BUDGETS: dict[str, int] = {
    "关于你": 1375,
    "事实与约定": 2200,
    "资料目录": 1500,
    "用户选择的技能": 40000,
}

# 内建工具不进注册表，按轮次规则单独校验（spec.md §7）。
BUILTIN_MESSAGE_ONLY = frozenset({"WebSearch", "WebFetch"})
BUILTIN_ATTACHMENT_ONLY = frozenset({"Read"})


def check_turn(evidence: TurnEvidence) -> list[Assertion]:
    """对一轮证据执行全部横切断言；附件信息取自证据本身。"""
    assertions: list[Assertion] = []
    run = evidence.run
    run_id = run.get("run_id")
    assertions.append(
        Assertion(
            "turn_done",
            run.get("status") == "done",
            f"status={run.get('status')} error={run.get('error')}",
        )
    )

    observation = evidence.observation
    assertions.append(
        Assertion(
            "observation_present",
            observation is not None,
            "" if observation is not None else f"run {run_id} 无轮次观测",
            infra=True,
        )
    )
    if observation is not None:
        for side in ("context_before", "context_after"):
            assertions.append(
                Assertion(
                    f"observation_{side}",
                    observation.get(side) is not None,
                    "上下文读数缺失",
                    infra=True,
                )
            )
        assertions.append(
            Assertion(
                "observation_single",
                sum(1 for r in evidence.all_observations if r.get("run_id") == run_id) == 1,
                "同一轮出现多份（或零份）观测",
                infra=True,
            )
        )
        assertions.extend(_check_material_budgets(observation))

    assertions.extend(_check_tool_exposure(evidence))
    return assertions


def check_grounded(
    evidence: TurnEvidence,
    *,
    expect: str,
    path: str,
    label: str = "knowledge",
) -> list[Assertion]:
    """确认成功返回了指定资料的事实片段；检索摘要与原文读取都可提供证据。"""
    calls = [call for call in evidence.invocations() if call.name in {"kb_search", "kb_read"}]
    succeeded = [call for call in calls if call.ok]
    grounded = []
    for call in succeeded:
        try:
            result = json.loads(call.result)
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict):
            continue
        candidates = result.get("results", []) if call.name == "kb_search" else [result]
        if not isinstance(candidates, list):
            continue
        if any(
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and item["path"].removeprefix("kb/") == path.removeprefix("kb/")
            and expect in str(item.get("snippet" if call.name == "kb_search" else "body") or "")
            for item in candidates
        ):
            grounded.append(call)
    return [
        Assertion(
            f"tool_success[{label}]",
            bool(succeeded),
            _invocation_detail(calls, succeeded, 160),
        ),
        Assertion(
            f"tool_grounded[{label}]",
            bool(grounded),
            f"未从 {path} 取得「{expect}」"
            if succeeded
            else f"没有成功的资料库查询或读取（共 {len(calls)} 次）",
        ),
    ]


def _invocation_detail(
    calls: list[ToolInvocation], succeeded: list[ToolInvocation], limit: int
) -> str:
    """工具断言失败时的说明：说清是没查资料，还是调用都失败了。"""
    if not calls:
        return "本轮没有任何资料库调用"
    if not succeeded:
        return "; ".join(
            f"{call.name} {call.status}：{_excerpt(call.result, limit)}" for call in calls
        )
    return f"有成功的调用但事实未取到（已调：{sorted({call.name for call in calls})}）"


def _excerpt(text: str, limit: int = 200) -> str:
    flattened = " ".join(text.split())
    return flattened[:limit] + ("…" if len(flattened) > limit else "")


def _check_material_budgets(observation: dict[str, Any]) -> list[Assertion]:
    materials = observation.get("materials") or {}
    assertions: list[Assertion] = []
    for entry in materials.get("assembled", []):
        title = str(entry.get("title", ""))
        budget = next((b for key, b in MATERIAL_CHAR_BUDGETS.items() if key in title), None)
        if budget is None:
            continue
        chars = int(entry.get("chars", 0))
        assertions.append(
            Assertion(
                f"material_budget[{title}]",
                chars <= budget,
                f"chars={chars} 超出 {budget}",
                infra=True,
            )
        )
    return assertions


def _check_tool_exposure(evidence: TurnEvidence) -> list[Assertion]:
    run = evidence.run
    try:
        allowed = ALLOWED_EFFECTS[TurnKind(run.get("kind"))]
    except ValueError:
        return [Assertion("turn_kind_known", False, f"未知轮次类型 kind={run.get('kind')}")]

    assertions: list[Assertion] = []
    for name, status in sorted(set(evidence.tool_items())):
        violation = _exposure_violation(
            name, kind=run.get("kind"), attachments=evidence.attachments, allowed=allowed
        )
        if violation is None:
            continue
        # 越权调用必须被拒绝（闸门生效）；真的执行成功才是红线违规。
        rejected = status != "ok"
        assertions.append(
            Assertion(
                f"tool_exposure[{name}]",
                rejected,
                f"{violation}；调用状态={status}",
                red_line=not rejected,
            )
        )
    return assertions


def _exposure_violation(
    name: str, *, kind: str | None, attachments: bool, allowed: frozenset
) -> str | None:
    """返回该工具在该轮次不该出现的理由；合规返回 None。"""
    if name in BUILTIN_MESSAGE_ONLY:
        if kind != "message":
            return f"{name} 仅用户亲自发起的轮可用"
        return None
    if name in BUILTIN_ATTACHMENT_ONLY:
        if kind != "message" or not attachments:
            return f"{name} 仅带附件的用户轮可用（attachments={attachments}）"
        return None
    tool = default_registry.get_tool(name)
    if tool is None:
        return "未注册工具"
    if tool.side_effect not in allowed:
        return f"side_effect={tool.side_effect.value} 不在该轮允许集"
    return None
