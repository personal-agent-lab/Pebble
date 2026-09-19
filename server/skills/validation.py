"""Skill 哈希计算与格式校验。"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from server.skills.models import Skill, SkillDraft, SkillInput

SKILL_ID_PATTERN = re.compile(r"^sk_[a-zA-Z0-9]+$")
DRAFT_ID_PATTERN = re.compile(r"^dr_[a-zA-Z0-9]+$")

# 影响执行的字段，用于 content_hash 计算
_HASH_FIELDS = (
    "body",
    "triggers",
    "inputs",
    "tools",
    "side_effects",
    "requires_confirmation",
)


def compute_hash(
    *,
    body: str = "",
    triggers: list[str] | None = None,
    inputs: list[dict[str, Any]] | None = None,
    tools: list[str] | None = None,
    side_effects: list[str] | None = None,
    requires_confirmation: bool = False,
) -> str:
    """计算内容哈希：覆盖影响执行的正文与元数据，排除管理字段。"""
    import json
    from dataclasses import asdict

    values = dict(
        body=body.strip(),
        triggers=triggers or [],
        inputs=[asdict(i) if isinstance(i, SkillInput) else i for i in (inputs or [])],
        tools=tools or [],
        side_effects=side_effects or [],
        requires_confirmation=requires_confirmation,
    )
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def compute_skill_hash(skill: Skill) -> str:
    """从 Skill 对象计算 content_hash。"""
    inputs_data = []
    for inp in skill.inputs:
        inputs_data.append({"name": inp.name, "required": inp.required, "note": inp.note})
    return (
        "sha256:"
        + hashlib.sha256(
            (
                skill.name
                + "\0"
                + skill.description
                + "\0"
                + compute_hash(
                    body=skill.body,
                    triggers=skill.triggers,
                    inputs=inputs_data,
                    tools=skill.tools,
                    side_effects=skill.side_effects,
                    requires_confirmation=skill.requires_confirmation,
                )
            ).encode()
        ).hexdigest()
    )


def validate_skill_id(skill_id: str) -> bool:
    """校验 Skill ID 格式：sk_[a-zA-Z0-9]+。"""
    return bool(SKILL_ID_PATTERN.match(skill_id))


def validate_draft_id(draft_id: str) -> bool:
    """校验草稿 ID 格式：dr_[a-zA-Z0-9]+。"""
    return bool(DRAFT_ID_PATTERN.match(draft_id))


def validate_skill_format(skill: Skill) -> list[dict[str, str]]:
    """校验 Skill 格式，返回字段错误列表。"""
    errors: list[dict[str, str]] = []
    if not skill.id:
        errors.append({"field": "id", "message": "id 不能为空"})
    elif not validate_skill_id(skill.id):
        errors.append({"field": "id", "message": "id 格式不合法，应为 sk_[a-zA-Z0-9]+"})
    if not skill.name:
        errors.append({"field": "name", "message": "name 不能为空"})
    if not skill.description:
        errors.append({"field": "description", "message": "description 不能为空"})
    return errors


def validate_draft_format(draft: SkillDraft) -> list[dict[str, str]]:
    """校验草稿格式，返回字段错误列表。"""
    errors = validate_skill_format(draft.skill)
    if not draft.draft_id:
        errors.append({"field": "draft_id", "message": "draft_id 不能为空"})
    elif not validate_draft_id(draft.draft_id):
        errors.append({"field": "draft_id", "message": "draft_id 格式不合法，应为 dr_[a-zA-Z0-9]+"})
    return errors
