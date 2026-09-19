"""受控的每轮 Skill 目录与实际加载审计。"""

from contextvars import ContextVar
from dataclasses import dataclass, field

from server.errors import NotFoundError, SkillValidationError, VersionConflictError
from server.skills import catalog


@dataclass
class Scope:
    run_id: str | None
    excluded: set[str]
    auto: bool
    manual: set[str]
    path: object = None
    loaded: set[str] = field(default_factory=set)


current: ContextVar[Scope | None] = ContextVar("skill_scope", default=None)


def read(skill_id: str, revision: str | None = None, source: str = "auto", audit: bool = True):
    scope = current.get()
    if scope and (skill_id in scope.excluded or (not scope.auto and skill_id not in scope.manual)):
        raise NotFoundError(skill_id)
    skill = next((s for s in catalog.available_catalog() if s.id == skill_id), None)
    if skill is None:
        raise NotFoundError(skill_id)
    if revision is not None and skill.content_hash != revision:
        raise VersionConflictError(skill.content_hash)
    if audit and scope and scope.run_id:
        catalog.record_skill_usage(scope.run_id, skill.id, skill.content_hash, source, scope.path)
        scope.loaded.add(skill.id)
    return skill


def validate_refs(refs: list[dict]) -> None:
    if len(refs) > 10:
        raise SkillValidationError([{"field": "skills", "message": "最多选择 10 个 Skill"}])
    total = 0
    for ref in refs:
        skill = read(ref["id"], ref.get("revision"), audit=False)
        total += len(skill.body)
    if total > 40000:
        raise SkillValidationError(
            [{"field": "skills", "message": "所选 Skill 正文超过 40000 字符"}]
        )
