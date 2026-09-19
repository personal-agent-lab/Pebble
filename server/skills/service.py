"""Skill 业务服务：创建、编辑、审批、停用、恢复。"""

from __future__ import annotations

from datetime import UTC

from server.errors import (
    NotEditableError,
    NotFoundError,
    SkillValidationError,
    VersionConflictError,
)
from server.skills import repository
from server.skills.models import (
    Skill,
    SkillDraft,
    SkillEvidence,
    SkillInput,
    SkillMeta,
    SkillSource,
    SkillStatus,
    SkillVersion,
)
from server.skills.validation import validate_skill_format


def list_skills(status: SkillStatus | None = None, search: str | None = None) -> list[SkillMeta]:
    """搜索与状态筛选。"""
    if status == SkillStatus.DRAFT:
        drafts = repository.list_drafts()
        return [
            SkillMeta(
                id=d.skill.id,
                name=d.skill.name,
                description=d.skill.description,
                status=d.skill.status,
                source=d.skill.source,
                content_hash=d.skill.content_hash,
                created_at=d.created_at,
                updated_at=d.updated_at,
            )
            for d in drafts
        ]
    all_skills = repository.list_all()
    if status is not None:
        all_skills = [s for s in all_skills if s.status == status]
    if search:
        q = search.lower()
        all_skills = [s for s in all_skills if q in s.name.lower() or q in s.description.lower()]
    return all_skills


def get_skill(skill_id: str) -> Skill:
    """获取完整 Skill，含正文。"""
    skill = repository.load_skill(skill_id)
    if skill is None:
        raise NotFoundError(f"Skill 不存在: {skill_id}")
    return repository.load_skill(skill.id)


@repository.locked
def create_skill(
    *,
    id: str | None = None,
    name: str,
    description: str,
    body: str = "",
    triggers: list[str] | None = None,
    inputs: list[dict] | None = None,
    tools: list[str] | None = None,
    side_effects: list[str] | None = None,
    requires_confirmation: bool = False,
) -> Skill:
    """用户创建 Skill 并直接批准。"""
    skill_id = id or repository.generate_id("sk")
    if repository.load_skill(skill_id) is not None:
        raise VersionConflictError(repository.load_skill(skill_id).content_hash)
    now = _now()
    skill_inputs = []
    for inp in inputs or []:
        if isinstance(inp, dict):
            skill_inputs.append(
                SkillInput(
                    name=inp.get("name", ""),
                    required=inp.get("required", True),
                    note=inp.get("note", ""),
                )
            )
    skill = Skill(
        id=skill_id,
        name=name,
        description=description,
        status=SkillStatus.APPROVED,
        source=SkillSource.USER,
        triggers=triggers or [],
        inputs=skill_inputs,
        tools=tools or [],
        side_effects=side_effects or [],
        requires_confirmation=requires_confirmation,
        body=body,
        created_at=now,
        updated_at=now,
    )
    errors = validate_skill_format(skill)
    if errors:
        raise SkillValidationError(errors)
    repository.save_skill(skill)
    return repository.load_skill(skill.id)


@repository.locked
def update_skill(
    skill_id: str,
    expected_revision: str,
    *,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
    triggers: list[str] | None = None,
    inputs: list[dict] | None = None,
    tools: list[str] | None = None,
    side_effects: list[str] | None = None,
    requires_confirmation: bool | None = None,
) -> Skill:
    """带版本校验的编辑保存。"""
    existing = repository.load_skill(skill_id)
    if existing is None:
        raise NotFoundError(f"Skill 不存在: {skill_id}")
    if existing.status == SkillStatus.ARCHIVED:
        raise NotEditableError(existing.status)
    if existing.content_hash != expected_revision:
        raise VersionConflictError(existing.content_hash)

    now = _now()
    skill_inputs = existing.inputs
    if inputs is not None:
        skill_inputs = []
        for inp in inputs:
            if isinstance(inp, dict):
                skill_inputs.append(
                    SkillInput(
                        name=inp.get("name", ""),
                        required=inp.get("required", True),
                        note=inp.get("note", ""),
                    )
                )

    updated = Skill(
        id=skill_id,
        name=name if name is not None else existing.name,
        description=description if description is not None else existing.description,
        status=SkillStatus.APPROVED,
        source=existing.source,
        schema_version=existing.schema_version,
        triggers=triggers if triggers is not None else existing.triggers,
        inputs=skill_inputs,
        tools=tools if tools is not None else existing.tools,
        side_effects=side_effects if side_effects is not None else existing.side_effects,
        requires_confirmation=(
            requires_confirmation
            if requires_confirmation is not None
            else existing.requires_confirmation
        ),
        evidence=existing.evidence,
        base_revision=existing.base_revision,
        approved_version=existing.approved_version,
        approved_at=existing.approved_at,
        body=body if body is not None else existing.body,
        created_at=existing.created_at,
        updated_at=now,
    )
    errors = validate_skill_format(updated)
    if errors:
        raise SkillValidationError(errors)
    repository.save_skill(updated)
    return repository.load_skill(updated.id)


@repository.locked
def disable_skill(skill_id: str) -> Skill:
    """停用 Skill。"""
    skill = repository.load_skill(skill_id)
    if skill is None:
        raise NotFoundError(f"Skill 不存在: {skill_id}")
    if skill.status == SkillStatus.ARCHIVED:
        raise NotEditableError(skill.status)
    updated = Skill(**{**skill.__dict__, "status": SkillStatus.DISABLED, "updated_at": _now()})
    repository.save_status(updated)
    return repository.load_skill(updated.id)


@repository.locked
def enable_skill(skill_id: str) -> Skill:
    """启用 Skill。"""
    skill = repository.load_skill(skill_id)
    if skill is None:
        raise NotFoundError(f"Skill 不存在: {skill_id}")
    from server.skills.validation import compute_skill_hash

    if skill.status == SkillStatus.ARCHIVED:
        raise NotEditableError(skill.status)
    if (
        skill.content_hash != compute_skill_hash(skill)
        or skill.approved_version != skill.content_hash
    ):
        raise VersionConflictError(compute_skill_hash(skill))
    updated = Skill(**{**skill.__dict__, "status": SkillStatus.APPROVED, "updated_at": _now()})
    repository.save_status(updated)
    return repository.load_skill(updated.id)


@repository.locked
def archive_skill(skill_id: str) -> None:
    """归档 Skill。"""
    skill = repository.load_skill(skill_id)
    if skill is None:
        raise NotFoundError(f"Skill 不存在: {skill_id}")
    repository.move_to_archive(skill_id)


@repository.locked
def create_draft(
    *,
    draft_id: str | None = None,
    skill_id: str | None = None,
    base_revision: str | None = None,
    name: str,
    description: str,
    body: str = "",
    triggers: list[str] | None = None,
    inputs: list[dict] | None = None,
    tools: list[str] | None = None,
    side_effects: list[str] | None = None,
    requires_confirmation: bool = False,
    evidence: dict | None = None,
) -> SkillDraft:
    """创建草稿（Agent 或用户）。"""
    did = draft_id or repository.generate_id("dr")
    now = _now()
    skill_inputs = []
    for inp in inputs or []:
        if isinstance(inp, dict):
            skill_inputs.append(
                SkillInput(
                    name=inp.get("name", ""),
                    required=inp.get("required", True),
                    note=inp.get("note", ""),
                )
            )

    skill_evidence = None
    if evidence:
        skill_evidence = SkillEvidence(
            tasks=evidence.get("tasks", []),
            occurrences=evidence.get("occurrences", 0),
            last_seen_at=evidence.get("last_seen_at", ""),
        )

    if skill_id:
        base_revision = get_skill(skill_id).content_hash
    sid = skill_id or repository.generate_id("sk")
    skill = Skill(
        id=sid,
        name=name,
        description=description,
        status=SkillStatus.DRAFT,
        source=SkillSource.DISTILLED if evidence else SkillSource.USER,
        triggers=triggers or [],
        inputs=skill_inputs,
        tools=tools or [],
        side_effects=side_effects or [],
        requires_confirmation=requires_confirmation,
        evidence=skill_evidence,
        base_revision=base_revision,
        body=body,
        created_at=now,
        updated_at=now,
    )
    draft = SkillDraft(
        draft_id=did,
        skill_id=sid,
        base_revision=base_revision,
        skill=skill,
        created_at=now,
        updated_at=now,
    )
    repository.save_draft(draft)
    return repository.load_draft(draft.draft_id)


@repository.locked
def update_draft(
    draft_id: str,
    expected_revision: str,
    *,
    name: str | None = None,
    description: str | None = None,
    body: str | None = None,
    triggers: list[str] | None = None,
    inputs: list[dict] | None = None,
    tools: list[str] | None = None,
    side_effects: list[str] | None = None,
    requires_confirmation: bool | None = None,
) -> SkillDraft:
    """带版本校验的草稿编辑。"""
    existing = repository.load_draft(draft_id)
    if existing is None:
        raise NotFoundError(f"草稿不存在: {draft_id}")
    if existing.skill.status != SkillStatus.DRAFT:
        raise NotEditableError(existing.skill.status)
    if existing.skill.content_hash != expected_revision:
        raise VersionConflictError(existing.skill.content_hash)

    now = _now()
    old = existing.skill
    skill_inputs = old.inputs
    if inputs is not None:
        skill_inputs = []
        for inp in inputs:
            if isinstance(inp, dict):
                skill_inputs.append(
                    SkillInput(
                        name=inp.get("name", ""),
                        required=inp.get("required", True),
                        note=inp.get("note", ""),
                    )
                )

    updated_skill = Skill(
        id=old.id,
        name=name if name is not None else old.name,
        description=description if description is not None else old.description,
        status=SkillStatus.DRAFT,
        source=old.source,
        schema_version=old.schema_version,
        triggers=triggers if triggers is not None else old.triggers,
        inputs=skill_inputs,
        tools=tools if tools is not None else old.tools,
        side_effects=side_effects if side_effects is not None else old.side_effects,
        requires_confirmation=(
            requires_confirmation
            if requires_confirmation is not None
            else old.requires_confirmation
        ),
        evidence=old.evidence,
        base_revision=old.base_revision,
        body=body if body is not None else old.body,
        created_at=old.created_at,
        updated_at=now,
    )
    updated_draft = SkillDraft(
        draft_id=draft_id,
        skill_id=existing.skill_id,
        base_revision=existing.base_revision,
        skill=updated_skill,
        created_at=existing.created_at,
        updated_at=now,
    )
    repository.save_draft(updated_draft)
    return repository.load_draft(draft_id)


@repository.locked
def approve_draft(draft_id: str, expected_revision: str) -> Skill:
    """批准草稿，移入 skills/。"""
    draft = repository.load_draft(draft_id)
    if draft is None:
        raise NotFoundError(f"草稿不存在: {draft_id}")
    if draft.skill.content_hash != expected_revision:
        raise VersionConflictError(draft.skill.content_hash)
    current = repository.load_skill(draft.skill_id)
    if draft.skill.status != SkillStatus.DRAFT:
        if (
            draft.skill.status == SkillStatus.APPROVED
            and current
            and current.content_hash == expected_revision
        ):
            return current
        raise NotEditableError(draft.skill.status)
    if current and current.content_hash != draft.base_revision:
        raise VersionConflictError(current.content_hash)
    now = _now()
    approved = Skill(
        **{
            **draft.skill.__dict__,
            "status": SkillStatus.APPROVED,
            "updated_at": now,
        }
    )
    errors = validate_skill_format(approved)
    if errors:
        raise SkillValidationError(errors)
    repository.save_skill(approved)
    repository.delete_draft(draft_id, approved=True)
    return repository.load_skill(approved.id)


@repository.locked
def reject_draft(draft_id: str) -> None:
    """驳回草稿。"""
    draft = repository.load_draft(draft_id)
    if draft is None:
        raise NotFoundError(f"草稿不存在: {draft_id}")
    if draft.skill.status == SkillStatus.APPROVED:
        raise NotEditableError(draft.skill.status)
    repository.delete_draft(draft_id)


@repository.locked
def restore_version(skill_id: str, revision: str) -> SkillDraft:
    """从历史版本生成恢复草稿。"""
    versions = repository.list_versions(skill_id)
    target = None
    for v in versions:
        if v.revision == revision:
            target = v
            break
    if target is None:
        raise NotFoundError(f"版本不存在: {revision}")
    now = _now()
    restored_skill = Skill(
        **{
            **target.snapshot.__dict__,
            "status": SkillStatus.DRAFT,
            "base_revision": get_skill(skill_id).content_hash,
            "updated_at": now,
        }
    )
    draft = SkillDraft(
        draft_id=repository.generate_id("dr"),
        skill_id=skill_id,
        base_revision=get_skill(skill_id).content_hash,
        skill=restored_skill,
        created_at=now,
        updated_at=now,
    )
    repository.save_draft(draft)
    return repository.load_draft(draft.draft_id)


def get_versions(skill_id: str) -> list[SkillVersion]:
    """获取版本历史。"""
    return repository.list_versions(skill_id)


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
