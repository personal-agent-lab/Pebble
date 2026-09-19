"""Agent 可见的 Skill 工具：skill_list、skill_read、skill_propose、skill_find_evidence。"""

from __future__ import annotations

from server.skills import catalog, service
from server.skills.runtime import current, read
from server.tools.registry import SideEffect, tool


@tool(
    name="skill_list",
    description="返回可用 Skill 的名称、描述与状态。用于了解有哪些 Skill 可用。",
    side_effect=SideEffect.READONLY,
)
def skill_list() -> list[dict]:
    scope = current.get()
    items = [
        s
        for s in catalog.available_catalog()
        if not scope or (s.id not in scope.excluded and (scope.auto or s.id in scope.manual))
    ]
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "status": s.status.value,
            "triggers": s.triggers,
        }
        for s in items
    ]


@tool(
    name="skill_read",
    description="读取指定 Skill 的完整内容。返回 SKILL.md 的正文，供你按流程执行。",
    side_effect=SideEffect.READONLY,
)
def skill_read(skill_id: str) -> dict:
    skill = read(skill_id)
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "source": "approved",
        "revision": skill.content_hash,
        "body": skill.body,
        "triggers": skill.triggers,
        "tools": skill.tools,
        "side_effects": skill.side_effects,
        "requires_confirmation": skill.requires_confirmation,
    }


@tool(
    name="skill_propose",
    description=(
        "任务收尾时若发现可复用流程，先用 skill_find_evidence 查询。"
        "至少三个不同任务成功完成相同工具序列才提交草稿。"
        "抽象参数，不复制私人信息；优先改进已有 Skill。"
        "不能批准，提交后提示用户到 Skills 待审核页审阅。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
)
def skill_propose(
    name: str,
    description: str,
    body: str = "",
    triggers: list[str] | None = None,
    inputs: list[dict] | None = None,
    tools: list[str] | None = None,
    side_effects: list[str] | None = None,
    requires_confirmation: bool = False,
    evidence: dict | None = None,
    skill_id: str | None = None,
) -> dict:
    from server.skills.evidence import verify
    from server.skills.repository import list_drafts

    evidence = verify(evidence)
    for existing in list_drafts():
        if (
            existing.skill.name == name
            and existing.skill.evidence
            and existing.skill.evidence.tasks == evidence["tasks"]
        ):
            return {"draft_id": existing.draft_id, "status": "draft"}
    draft = service.create_draft(
        skill_id=skill_id,
        name=name,
        description=description,
        body=body,
        triggers=triggers,
        inputs=inputs,
        tools=tools,
        side_effects=side_effects,
        requires_confirmation=requires_confirmation,
        evidence=evidence,
    )
    return {
        "id": draft.skill_id,
        "draft_id": draft.draft_id,
        "path": f"skill_drafts/{draft.draft_id}/SKILL.md",
        "status": "draft",
    }


@tool(
    name="skill_find_evidence",
    description="查找与指定 Skill 相关的执行依据：工具调用序列、成功次数、来源任务标识。",
    side_effect=SideEffect.READONLY,
)
def skill_find_evidence(skill_id: str | None = None) -> dict:
    from server.skills.evidence import find

    return find()
