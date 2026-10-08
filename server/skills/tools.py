"""Agent 可见的技能工具：目录、按需读取与用户发起轮的修改。

技能语义全部写在这些工具的 description 里，不进基础提示。技能是说明书不是程序：
读取不等于执行，技能内容不授予任何外部操作权限；外部写仍走既有确认与轮次限制。
"""

from __future__ import annotations

import logging

from server.errors import SkillUnknownError, SkillValidationError
from server.skills.models import DESCRIPTION_LIMIT, ChangeAction, ChangeActor, SkillState
from server.skills.runtime import current
from server.skills.service import ChangeRequest, SkillService
from server.tools.registry import Effect, ToolPolicy, activity, tool

logger = logging.getLogger(__name__)


@tool(
    name="skill_list",
    effect=Effect.READ_ONLY,
    policy=ToolPolicy.ALL_TURNS,
    activity_renderer=lambda args: activity("正在查看技能目录"),
)
def skill_list(state: str = "active", *, skills: SkillService) -> list[dict]:
    """列出可用的技能目录：每个技能的标识、名称与一句描述。

    技能是此前沉淀的“这类任务该怎么做”的操作说明。本工具只给目录；判断某个技能与
    当前任务相关时，用 skill_view 读取它的正文与附件再照着执行，不相关就一个都不读。
    state 可选 active（默认）/stale/archived，一般不需要传。
    """
    try:
        parsed = SkillState(state)
    except ValueError as error:
        raise SkillValidationError([{"field": "state", "message": f"未知状态：{state}"}]) from error
    entries = skills.catalog(state=parsed)
    scope = current()
    if scope is not None:
        # 排除项与关闭自动匹配都由目录工具先挡一层：模型看不到，也就不会去读。
        entries = [
            entry for entry in entries if entry["skill_id"] not in scope.excluded_skill_ids
        ]
        if not scope.auto_match:
            entries = [
                entry for entry in entries if entry["skill_id"] in scope.manual_skill_ids
            ]
    return [
        {
            "skill_id": entry["skill_id"],
            "name": entry["name"],
            "description": entry["description"][:DESCRIPTION_LIMIT],
            "revision": entry["revision"],
        }
        for entry in entries
    ]


@tool(
    name="skill_view",
    effect=Effect.READ_ONLY,
    policy=ToolPolicy.ALL_TURNS,
    activity_renderer=lambda args: activity("正在读取技能", args.get("skill_id")),
)
def skill_view(skill_id: str, file_path: str | None = None, *, skills: SkillService) -> dict:
    """读取一个技能的正文，或它 references/、templates/ 下的一个附件。

    正文是适用场景、操作步骤、注意事项与验证方法；附件是更长的参考资料或模板，正文
    需要时才按文件读取。返回当前内容版本 revision 与附件清单。技能是说明书不是程序：
    读完仍通过既有工具完成操作，技能不授予任何额外权限。
    """
    scope = current()
    if scope is not None:
        if skill_id in scope.excluded_skill_ids:
            raise SkillUnknownError(f"本轮已排除技能：{skill_id}")
        if not scope.auto_match and skill_id not in scope.manual_skill_ids:
            raise SkillUnknownError(f"本轮已关闭自动匹配且未选择该技能：{skill_id}")
    skill = skills.loadable(skill_id)
    if file_path is not None:
        content = skills.repository.read_attachment(skill_id, file_path).decode("utf-8")
    else:
        content = skill.body
    if scope is not None and scope.task_id is not None and scope.run_id is not None:
        try:
            skills.record_load(scope.task_id, scope.run_id, skill, "auto")
        except Exception:
            # 使用记录是内部簿记：写不进去也不能让读取本身失败。
            logger.exception("技能 %s 的加载记录写入失败", skill_id)
    return {
        "skill_id": skill.skill_id,
        "name": skill.name,
        "description": skill.description,
        "revision": skill.revision,
        "body" if file_path is None else "file": content,
        "files": [
            {"relative_path": item.relative_path, "content_hash": item.content_hash}
            for item in skill.files
        ],
    }


def _manage_notice(result: dict) -> str:
    skill_id = (result.get("skill") or {}).get("skill_id") or result.get("skill_id", "")
    if result.get("status") == "proposed":
        return f"技能修改建议已生成，等待用户在管理页确认：{skill_id}"
    return f"技能变更已保存：{skill_id}"


@tool(
    name="skill_manage",
    effect=Effect.LOCAL_WRITE,
    policy=ToolPolicy.USER_TURN_ONLY,
    notice_renderer=_manage_notice,
    activity_renderer=lambda args: activity(
        "正在保存技能", (args.get("payload") or {}).get("skill_id")
    ),
)
def skill_manage(
    action: str,
    payload: dict,
    reason: str,
    expected_revision: str | None = None,
    *,
    skills: SkillService,
) -> dict:
    """创建或修改一个技能；只在用户亲自发起的对话轮可用（用户当轮明确要求时才调用）。

    用户说“把刚才的排查过程记成技能”“把这个做法更新进那个技能”时用本工具。沉淀的是
    做法，不是过程记录：正文写适用场景、操作步骤、注意事项与验证方法，不复制消息原文
    与一次性参数值。没有可靠经验就不要保存。

    参数：action 取 create / patch / write_file / remove_file；payload 随 action 而定——
    create 给 skill_id（小写字母数字与连字符，如 screen-cast）、name、description（一句
    话，≤160 字符）、body 与可选 attachments（{路径: 内容}，路径在 references/ 或
    templates/ 下）；patch 给 skill_id 与整份新 body，或 old_string/new_string（须在正文
    中恰好出现一次）；write_file 给 skill_id、relative_path、content；remove_file 给
    skill_id、relative_path。修改既有技能必须传 expected_revision（当前正文的内容版本），
    过期会返回 skill_conflict。reason 写为什么改，供用户审阅。
    """
    try:
        parsed_action = ChangeAction(action)
    except ValueError as error:
        message = f"未知动作：{action}"
        raise SkillValidationError([{"field": "action", "message": message}]) from error
    if parsed_action is not ChangeAction.CREATE and not expected_revision:
        raise SkillValidationError(
            [{"field": "expected_revision", "message": "修改既有技能必须先读取当前内容版本"}]
        )
    result = skills.record_change(
        ChangeRequest(
            action=parsed_action,
            payload=payload,
            actor=ChangeActor.FOREGROUND,
            reason=reason,
            skill_id=payload.get("skill_id"),
            base_revision=expected_revision,
        )
    )
    return result
