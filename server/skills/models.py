"""Skill 数据模型：状态、来源、元数据、完整 Skill、版本与草稿。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SkillStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class SkillSource(StrEnum):
    USER = "user"
    DISTILLED = "distilled"


@dataclass(frozen=True)
class SkillInput:
    """Skill 的一个必要参数。"""

    name: str
    required: bool = True
    note: str = ""


@dataclass(frozen=True)
class SkillEvidence:
    """Agent 总结时的来源依据。"""

    tasks: list[str] = field(default_factory=list)
    occurrences: int = 0
    last_seen_at: str = ""


@dataclass(frozen=True)
class SkillMeta:
    """Skill 元数据（不含正文），用于目录和搜索。"""

    id: str
    name: str
    description: str
    status: SkillStatus
    source: SkillSource
    schema_version: int = 1
    triggers: list[str] = field(default_factory=list)
    inputs: list[SkillInput] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    content_hash: str = ""
    base_revision: str | None = None
    approved_version: str | None = None
    approved_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Skill(SkillMeta):
    """完整 Skill，包含正文和证据。"""

    body: str = ""
    evidence: SkillEvidence | None = None


@dataclass(frozen=True)
class SkillVersion:
    """历史版本快照。"""

    skill_id: str
    revision: str
    snapshot: Skill
    approved_at: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class SkillDraft:
    """待审核草稿。"""

    draft_id: str
    skill_id: str | None = None
    base_revision: str | None = None
    skill: Skill = field(
        default_factory=lambda: Skill(
            id="", name="", description="", status=SkillStatus.DRAFT, source=SkillSource.USER
        )
    )
    created_at: str = ""
    updated_at: str = ""
