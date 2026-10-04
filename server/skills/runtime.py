"""技能装配运行时：本轮范围、目录材料与手动装配。

目录常驻（每轮注入容量受限的目录）与正文按需（模型用 skill_view 读取）在这里成形；
加载在装配或读取时绑定当前内容版本并记入 `skill_loads`。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

from server.agent.context import Material
from server.skills.models import DESCRIPTION_LIMIT, SkillState
from server.skills.service import (
    CATALOG_LIMIT,
    MANUAL_BODY_BUDGET,
    MANUAL_SKILL_LIMIT,
    SkillService,
)

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


def catalog_material(
    service: SkillService, *, excluded_skill_ids=(), observer=None
) -> Material | None:
    """目录材料：最多 CATALOG_LIMIT 条“标识+名称+一句描述”，超出按最近使用截取并注明。

    目录生成失败是关键静默降级：经 observer 记步骤并进材料摘要的跳过清单，
    没有目录可装配（无激活技能）不算跳过。
    """

    try:
        entries = [
            entry
            for entry in service.catalog(state=SkillState.ACTIVE)
            if entry["skill_id"] not in set(excluded_skill_ids)
        ]
    except Exception:
        logger.exception("技能目录装配失败，本轮不注入目录")
        if observer is not None:
            observer.degraded(
                "catalog_failed",
                {"catalog": "skills"},
                skipped={"category": "技能目录", "reason": "生成失败"},
            )
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


def _skip_skill(observer, skill_id: str, reason: str) -> None:
    if observer is None:
        return
    observer.degraded(
        "manual_skill_not_assembled",
        {"skill_id": skill_id, "reason": reason},
        skipped={"category": "技能", "skill_id": skill_id, "reason": reason},
    )


# 跳过原因的用户措辞：同一份进时间线通知与模型材料，不各说各话。
SKIP_REASONS = {
    "not_loadable": "文件有未收编的直接修改或已不可加载",
    "over_limit": f"手动选择的技能超过 {MANUAL_SKILL_LIMIT} 个的上限",
    "body_over_budget": f"正文合计超出 {MANUAL_BODY_BUDGET} 字符的装配预算",
}

SKIPPED_TITLE = "未装配的手动选择技能"
SKIPPED_NOTE = (
    "以下是用户本轮手动选择、但未能装配进上下文的技能；用户可能以为它们已生效，"
    "涉及相关内容时如实说明，不要假设这些技能的内容可用。"
)


def _display_name(service: SkillService, skill_id: str) -> str:
    """优先用技能名（管理面读取对脏技能同样有效），读不到再退回标识。"""

    try:
        return service.get(skill_id).name
    except Exception:
        return skill_id


def skipped_notice(service: SkillService, skipped: list[dict]) -> str:
    """程序写的时间线告知文案：跳过必须让用户看见（skills.md §9），不靠模型转述。"""

    listed = "、".join(
        f"《{_display_name(service, item['skill_id'])}》（{SKIP_REASONS[item['reason']]}）"
        for item in skipped
    )
    return f"你选择的技能未能装配：{listed}。本轮回复不包含它们的内容。"


def manual_materials(
    service: SkillService,
    skill_ids,
    *,
    task_id,
    run_id,
    observer=None,
    notify: Callable[[str], None] | None = None,
) -> list[Material]:
    """手动选择的技能正文：发送时绑定当前内容版本并记入加载记录（source=manual）。

    提交时已校验；这里再按同一额度兜底（额度在服务层与装配层都要成立），装配时目标
    已被删除、停用或磁盘被直接改动过的跳过并告警，不失败整轮。跳过经 observer 记
    降级步骤并进材料摘要的跳过清单；同时追加一份"未装配"材料让模型知情，并经
    notify 交付程序写的时间线告知——告知是程序保证，不依赖模型开口。
    """

    materials: list[Material] = []
    seen: set[str] = set()
    total = 0
    skipped: list[dict] = []
    for index, skill_id in enumerate(skill_ids):
        if skill_id in seen:
            continue
        seen.add(skill_id)
        if len(seen) > MANUAL_SKILL_LIMIT:
            logger.warning("手动选择的技能超过 %s 个，其余不装配", MANUAL_SKILL_LIMIT)
            rest = [skill_id, *(s for s in skill_ids[index + 1 :] if s not in seen)]
            for skipped_id in rest:
                _skip_skill(observer, skipped_id, "over_limit")
                skipped.append({"skill_id": skipped_id, "reason": "over_limit"})
            break
        try:
            skill = service.loadable(skill_id)
        except Exception:
            logger.warning("手动选择的技能 %s 不可用，本轮不装配", skill_id)
            _skip_skill(observer, skill_id, "not_loadable")
            skipped.append({"skill_id": skill_id, "reason": "not_loadable"})
            continue
        if total + len(skill.body) > MANUAL_BODY_BUDGET:
            logger.warning(
                "手动选择的技能正文超过 %s 字符，%s 不装配", MANUAL_BODY_BUDGET, skill_id
            )
            _skip_skill(observer, skill_id, "body_over_budget")
            skipped.append({"skill_id": skill_id, "reason": "body_over_budget"})
            continue
        total += len(skill.body)
        try:
            service.record_load(task_id, run_id, skill, "manual")
        except Exception:
            logger.exception("技能 %s 的加载记录写入失败", skill_id)
        materials.append(Material(title=f"用户选择的技能：{skill.name}", content=skill.body))
    if skipped:
        materials.append(
            Material(
                title=SKIPPED_TITLE,
                content={
                    "说明": SKIPPED_NOTE,
                    "skills": [
                        {
                            "skill_id": item["skill_id"],
                            "名称": _display_name(service, item["skill_id"]),
                            "原因": SKIP_REASONS[item["reason"]],
                        }
                        for item in skipped
                    ],
                },
            )
        )
        if notify is not None:
            notify(skipped_notice(service, skipped))
    return materials
