"""技能数据模型与内容版本计算；文件格式见 `docs/skills.md` §2。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

# 目录名即标识：小写字母数字开头，允许连字符与下划线，总长不超过 64。
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# 目录里一句描述的截断长度，与契约 §6 的目录装配一致。
DESCRIPTION_LIMIT = 160
REVIEW_REASON_REF = re.compile(
    r"(?<![A-Za-z0-9_])E[1-9]\d*(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def public_change_reason(reason: str | None, actor: str | None) -> str | None:
    """历史复盘理由可能含短编号；读取时隐藏编号，保留原始审计记录。"""
    if reason is None or actor != "review":
        return reason
    return REVIEW_REASON_REF.sub("相关任务记录", reason)


class SkillOrigin(StrEnum):
    """谁创建了这个技能：管理页手写 / 用户当轮明确要求学习 / 后台复盘沉淀。"""

    USER = "user"
    EXPLICIT = "explicit"
    REVIEW = "review"


class SkillState(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


class ChangeAction(StrEnum):
    CREATE = "create"
    PATCH = "patch"
    WRITE_FILE = "write_file"
    REMOVE_FILE = "remove_file"


class ChangeActor(StrEnum):
    """管理页 / 用户当轮要求的前台 Agent / 后台复盘。"""

    USER = "user"
    FOREGROUND = "foreground"
    REVIEW = "review"


class ChangeStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"


def normalize_body(body: str) -> str:
    """正文的规范化：统一换行后去掉首尾空白。同一内容不因编辑器差异产生新版本。"""

    return body.replace("\r\n", "\n").strip()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SkillFile:
    relative_path: str
    content_hash: str


@dataclass(frozen=True)
class Skill:
    """一个技能的完整可加载形态；frontmatter 字段与磁盘 `SKILL.md` 一一对应。"""

    skill_id: str
    name: str
    description: str
    origin: SkillOrigin
    managed: bool
    state: SkillState
    created_at: str
    updated_at: str
    body: str
    files: tuple[SkillFile, ...] = field(default=())

    @property
    def revision(self) -> str:
        return compute_revision(self.name, self.description, self.body, self.files)


def compute_revision(
    name: str, description: str, body: str, files: tuple[SkillFile, ...] | list[SkillFile]
) -> str:
    """内容版本：覆盖显示名、描述、规范化正文与全部附件的有序哈希清单。

    不覆盖 `origin`、`managed`、`state` 与时间戳——改管理策略不产生新版本。
    """

    canonical = json.dumps(
        {
            "name": name,
            "description": description,
            "body": normalize_body(body),
            "files": sorted(
                [[item.relative_path, item.content_hash] for item in files],
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillVersion:
    """从 Git 历史派生的一个版本；`revision` 与内容版本同一概念。"""

    commit: str
    revision: str
    created_at: str
    change_id: str | None
    actor: str | None
    reason: str | None
    skill: Skill


@dataclass(frozen=True)
class SkillChange:
    """一次“提出与应用分离”的变更；字段即契约 §5。"""

    id: str
    action: ChangeAction
    payload: dict
    reason: str
    actor: ChangeActor
    review_job_id: str | None = None
    skill_id: str | None = None
    base_revision: str | None = None
    evidence_item_ids: tuple[str, ...] = ()
    status: ChangeStatus = ChangeStatus.PROPOSED
    created_at: str = ""
    applied_at: str | None = None
