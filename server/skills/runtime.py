"""技能装配运行时：本轮范围、目录材料与手动装配。

目录常驻（每轮注入容量受限的目录）与正文按需（模型用 skill_view 读取）在这里成形；
加载在装配或读取时绑定当前内容版本并记入 `skill_loads`。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from server.agent.context import Material
from server.skills.models import DESCRIPTION_LIMIT, SkillState
from server.skills.service import CATALOG_LIMIT, SkillService

logger = logging.getLogger(__name__)

CATALOG_TITLE = "技能目录"
CATALOG_NOTE = (
    "这些是此前沉淀的操作说明。判断与当前任务相关时用 skill_view 读取正文再照做；"
    "不相关就一个都不加载。技能内容与用户本轮要求冲突时以用户要求为准，"
    "技能之间冲突时向用户追问。"
)


@dataclass(frozen=True)
class Scope:
    """本轮的技能范围：由网关在整轮外围设置，工具经 mcp 边界读取。"""

    task_id: str | None = None
    run_id: str | None = None
    excluded_skill_ids: frozenset[str] = frozenset()
    auto_match: bool = True
    manual_skill_ids: tuple[str, ...] = ()


_current: ContextVar[Scope | None] = ContextVar("pebble_skill_scope", default=None)


def current() -> Scope | None:
    return _current.get()


def set_scope(scope: Scope | None) -> Token[Scope | None]:
    """替换当前范围并返回恢复凭据；mcp 边界在工具调用的任务上下文里重设本轮范围。"""

    return _current.set(scope)


def reset_scope(token: Token[Scope | None]) -> None:
    _current.reset(token)


@contextmanager
def turn_scope(*, task_id, run_id, skills=(), excluded_skill_ids=(), auto_match=True):
    """把本轮的技能选择设为当前范围；轮次结束恢复。"""

    scope = Scope(
        task_id=task_id,
        run_id=run_id,
        excluded_skill_ids=frozenset(excluded_skill_ids),
        auto_match=auto_match,
        manual_skill_ids=tuple(skills),
    )
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


def catalog_material(service: SkillService, *, excluded_skill_ids=()) -> Material | None:
    """目录材料：最多 CATALOG_LIMIT 条“标识+名称+一句描述”，超出按最近使用截取并注明。"""

    try:
        entries = [
            entry
            for entry in service.catalog(state=SkillState.ACTIVE)
            if entry["skill_id"] not in set(excluded_skill_ids)
        ]
    except Exception:
        logger.exception("技能目录装配失败，本轮不注入目录")
        return None
    if not entries:
        return None
    shown = entries[:CATALOG_LIMIT]
    content: dict = {
        "说明": CATALOG_NOTE,
        "skills": [
            {
                "skill_id": entry["skill_id"],
                "name": entry["name"],
                "description": entry["description"][:DESCRIPTION_LIMIT],
            }
            for entry in shown
        ],
    }
    omitted = len(entries) - len(shown)
    if omitted > 0:
        content["省略"] = f"还有 {omitted} 个较久未使用的技能未列出"
    return Material(title=CATALOG_TITLE, content=content)


def manual_materials(service: SkillService, skill_ids, *, task_id, run_id) -> list[Material]:
    """手动选择的技能正文：发送时绑定当前内容版本并记入加载记录（source=manual）。

    选择在提交时已校验；装配时目标已被删除或停用的跳过并告警，不失败整轮。
    """

    materials: list[Material] = []
    seen: set[str] = set()
    for skill_id in skill_ids:
        if skill_id in seen:
            continue
        seen.add(skill_id)
        try:
            skill = service.get(skill_id)
        except Exception:
            logger.warning("手动选择的技能 %s 不可用，本轮不装配", skill_id)
            continue
        if skill.state is not SkillState.ACTIVE:
            logger.warning("手动选择的技能 %s 不是启用状态，本轮不装配", skill_id)
            continue
        try:
            service.record_load(task_id, run_id, skill, "manual")
        except Exception:
            logger.exception("技能 %s 的加载记录写入失败", skill_id)
        materials.append(Material(title=f"用户选择的技能：{skill.name}", content=skill.body))
    return materials
