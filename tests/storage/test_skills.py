"""Skill 生命周期、文件历史、失败恢复和受控加载。"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from server.errors import NotFoundError, SkillValidationError, VersionConflictError
from server.skills import catalog, repository, service
from server.skills.models import SkillStatus
from server.skills.runtime import Scope, current, read, validate_refs
from server.skills.validation import compute_skill_hash


def create():
    return service.create_skill(
        name="会议整理", description="整理会议", body="  按目标查询，然后核对结果。\n"
    )


def test_lifecycle_and_history(settings):
    skill = create()
    assert skill.content_hash == compute_skill_hash(skill)
    assert catalog.available_catalog()[0].body == skill.body
    draft = service.create_draft(
        skill_id=skill.id, name=skill.name, description=skill.description, body="新流程"
    )
    assert draft.base_revision == skill.content_hash
    assert service.get_skill(skill.id).body == skill.body
    approved = service.approve_draft(draft.draft_id, draft.skill.content_hash)
    assert approved.body == "新流程"
    assert service.approve_draft(draft.draft_id, draft.skill.content_hash) == approved
    assert repository.list_drafts() == []
    service.disable_skill(skill.id)
    assert not catalog.available_catalog()
    assert service.list_skills(SkillStatus.DISABLED)
    with pytest.raises(NotFoundError):
        read(skill.id)
    service.enable_skill(skill.id)
    service.archive_skill(skill.id)
    assert service.list_skills(SkillStatus.ARCHIVED)
    assert not catalog.available_catalog()
    restored = service.restore_version(skill.id, skill.content_hash)
    assert restored.skill.body == skill.body
    service.approve_draft(restored.draft_id, restored.skill.content_hash)
    assert read(skill.id).body == skill.body
    assert not (settings.data_dir / "skill_archives" / skill.id / "SKILL.md").exists()


def test_conflict_and_tampering(settings):
    skill = create()
    draft = service.create_draft(skill_id=skill.id, name="建议", description="说明", body="候选")
    changed = service.update_skill(skill.id, skill.content_hash, name="改名")
    assert changed.content_hash != skill.content_hash
    with pytest.raises(VersionConflictError):
        service.update_skill(skill.id, skill.content_hash, body="旧页面")
    with pytest.raises(VersionConflictError):
        service.approve_draft(draft.draft_id, draft.skill.content_hash)
    path = settings.data_dir / "skills" / skill.id / "SKILL.md"
    path.write_text(path.read_text() + "\n未审核内容")
    assert catalog.available_catalog() == []
    with pytest.raises(NotFoundError):
        read(skill.id)


def test_draft_revision_and_scope(settings):
    skill = create()
    token = current.set(Scope(None, {skill.id}, True, set()))
    try:
        with pytest.raises(NotFoundError):
            read(skill.id)
    finally:
        current.reset(token)
    with pytest.raises(VersionConflictError):
        validate_refs([dict(id=skill.id, revision="old")])
    draft = service.create_draft(name="草稿", description="描述", body="正文")
    assert draft.skill.content_hash
    with pytest.raises(VersionConflictError):
        service.update_draft(draft.draft_id, "old", body="不同正文")
    with pytest.raises(SkillValidationError):
        service.create_draft(name="", description="", body="")


def test_git_failure_rolls_back(settings, monkeypatch):
    skill = create()
    original = subprocess.run

    def failing(args, **kwargs):
        if args[:2] == ["git", "commit"]:
            raise subprocess.CalledProcessError(1, args)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(subprocess.CalledProcessError):
        service.update_skill(skill.id, skill.content_hash, body="不能发布")
    assert read(skill.id).body == skill.body


def test_interrupted_write_recovers(settings):
    skill = create()
    rel = f"skills/{skill.id}/SKILL.md"
    path = settings.data_dir / rel
    old = path.read_text()
    new = old + "\n未提交"
    (settings.data_dir / ".skills-journal.json").write_text(
        json.dumps({rel: dict(old=old, new=new)})
    )
    path.write_text(new)
    assert read(skill.id).body == skill.body
    assert path.read_text() == old


def test_concurrent_edits_only_one_wins(settings):
    skill = create()

    def edit(body):
        try:
            service.update_skill(skill.id, skill.content_hash, body=body)
            return True
        except VersionConflictError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, ["one", "two"])) == [False, True]


def test_symlinks_and_hash_inputs(settings, tmp_path):
    skill = create()
    from server.skills.models import SkillInput

    assert compute_skill_hash(
        replace(skill, inputs=[SkillInput("x", note="a")])
    ) != compute_skill_hash(replace(skill, inputs=[SkillInput("x", note="b")]))
    (settings.data_dir / "skills" / "sk_escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        repository.load_skill("sk_escape")
