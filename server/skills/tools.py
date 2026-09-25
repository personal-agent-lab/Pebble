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
        "仅当用户在当前对话中明确要求把某项已完成工作总结为 Skill 时调用；"
        "不要自行判断工作是否值得保存，也不要主动建议保存。"
        "先理解用户指定的是当前任务还是过去的任务；"
        "过去的任务用 history_search/history_read 核对内容。"
        "用简洁标题作 name，概括适用条件、可复用步骤和验证方法，抽象参数且不复制私人信息。"
        "source_task_id 可省略以指当前任务；服务端核对该任务在本轮前已有完成记录。"
        "只提交待审核草稿，不能批准；提交后提示用户到 Skills 页面审核。"
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
    source_task_id: str | None = None,
    skill_id: str | None = None,
) -> dict:
    from server.errors import SkillValidationError
    from server.skills.evidence import verify
    from server.skills.repository import list_drafts

    if not name.strip() or len(name.strip()) > 30:
        raise SkillValidationError(
            [{"field": "name", "message": "Skill 标题须简洁，不超过 30 个字符"}]
        )
    if not description.strip() or not body.strip():
        raise SkillValidationError(
            [{"field": "body", "message": "Skill 需要描述和可复用的流程正文"}]
        )
    name = name.strip()
    evidence = verify(source_task_id)
    for existing in list_drafts():
        if (
            existing.skill.name == name
            and existing.skill.description == description
            and existing.skill.body == body
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
    description="用户要求总结已完成工作时，查询当前或指定任务的已完成轮次；不判断是否值得保存。",
    side_effect=SideEffect.READONLY,
)
def skill_find_evidence(source_task_id: str | None = None) -> dict:
    from server.skills.evidence import find

    return find(source_task_id)
