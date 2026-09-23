"""技能存储层：frontmatter 往返、内容版本、附件路径安全、提交回滚与版本历史。"""

from dataclasses import replace

import pytest

from server.skills.models import (
    Skill,
    SkillFile,
    SkillOrigin,
    SkillState,
    compute_revision,
)
from server.skills.repository import (
    SkillRepository,
    parse_skill_md,
    safe_attachment_path,
    skill_dir_path,
)


def make_skill(skill_id: str = "weekly-report", **overrides) -> Skill:
    fields = dict(
        skill_id=skill_id,
        name="周报整理",
        description="按固定分节整理本周周报",
        origin=SkillOrigin.USER,
        managed=False,
        state=SkillState.ACTIVE,
        created_at="2026-09-23T00:00:00+00:00",
        updated_at="2026-09-23T00:00:00+00:00",
        body="## 适用场景\n\n整理周报时使用。\n",
        files=(),
    )
    fields.update(overrides)
    return Skill(**fields)


@pytest.fixture
def repo(settings) -> SkillRepository:
    repository = SkillRepository(settings.data_dir)
    repository.initialize()
    return repository


# ---------------------------------------------------------------- 模型与版本


def test_revision_ignores_management_fields_but_follows_content() -> None:
    base = make_skill(files=(SkillFile("references/a.md", "hash-a"),))
    same = replace(base, managed=True, state=SkillState.STALE)
    assert compute_revision(same.name, same.description, same.body, same.files) == base.revision

    assert replace(base, body="换了正文").revision != base.revision
    assert replace(base, name="新名字").revision != base.revision
    assert replace(base, description="新描述").revision != base.revision
    assert replace(base, files=(SkillFile("references/a.md", "hash-b"),)).revision != base.revision
    assert replace(base, files=(SkillFile("references/b.md", "hash-a"),)).revision != base.revision


def test_revision_normalizes_line_endings_and_padding() -> None:
    assert compute_revision("名", "描述", "正文\r\n内容\n\n\n", ()) == compute_revision(
        "名", "描述", "正文\n内容", ()
    )


def test_skill_id_and_attachment_path_validation() -> None:
    assert skill_dir_path("weekly-report") == "skills/weekly-report"
    assert skill_dir_path("a" * 64) == f"skills/{'a' * 64}"
    for bad in ("Weekly", "_x", "-x", "a b", "a/b", "", "a" * 65):
        with pytest.raises(ValueError):
            skill_dir_path(bad)

    assert safe_attachment_path("references/a.md") == "references/a.md"
    assert safe_attachment_path("templates/t/b.txt") == "templates/t/b.txt"
    for bad in (
        "../SKILL.md",
        "references/../SKILL.md",
        "/etc/passwd",
        "other/a.md",
        "references",
        "",
    ):
        with pytest.raises(ValueError):
            safe_attachment_path(bad)


# ---------------------------------------------------------------- 存取与往返


def test_skill_md_roundtrip(repo: SkillRepository) -> None:
    skill = make_skill()
    repo.commit_skill(
        skill, "[Skills] create weekly-report\n\nchange-id: ch1\nactor: user\nreason: 创建"
    )
    loaded = repo.load("weekly-report")
    assert loaded is not None
    assert (loaded.name, loaded.description) == (skill.name, skill.description)
    assert (loaded.origin, loaded.managed, loaded.state) == (
        skill.origin,
        skill.managed,
        skill.state,
    )
    assert loaded.body == skill.body
    assert loaded.revision == skill.revision
    assert loaded.files == ()


def test_attachments_join_revision_and_read_back(repo: SkillRepository) -> None:
    skill = make_skill()
    repo.commit_skill(
        skill,
        "[Skills] create weekly-report",
        {"references/notes.md": b"# notes\n", "templates/tpl.txt": b"tpl"},
    )
    loaded = repo.load("weekly-report")
    assert loaded is not None
    assert [f.relative_path for f in loaded.files] == ["references/notes.md", "templates/tpl.txt"]
    assert loaded.revision != skill.revision
    assert repo.read_attachment("weekly-report", "references/notes.md") == b"# notes\n"

    # 附件变化产生新版本；删除附件回到无附件版本。
    first = loaded.revision
    repo.commit_skill(
        make_skill(), "[Skills] write_file weekly-report", {"references/notes.md": b"# changed\n"}
    )
    assert repo.load("weekly-report").revision != first
    repo.commit_writes(
        [("skills/weekly-report/templates/tpl.txt", None)],
        "[Skills] remove_file weekly-report",
    )
    after = repo.load("weekly-report")
    assert after is not None
    assert [f.relative_path for f in after.files] == ["references/notes.md"]


def test_read_attachment_rejects_escape_and_symlinks(repo: SkillRepository, tmp_path) -> None:
    repo.commit_skill(make_skill(), "[Skills] create weekly-report", {"references/ok.md": b"ok"})
    from server.errors import SkillUnknownError

    with pytest.raises(SkillUnknownError):
        repo.read_attachment("weekly-report", "../SKILL.md")
    with pytest.raises(SkillUnknownError):
        repo.read_attachment("weekly-report", "references/missing.md")

    link = settings_symlink_target(tmp_path)
    import os

    os.symlink(link, repo.skills_root / "weekly-report" / "references" / "escape.md")
    with pytest.raises(SkillUnknownError):
        repo.read_attachment("weekly-report", "references/escape.md")


def settings_symlink_target(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    return target


def test_unparseable_or_dirty_skill_leaves_catalog(repo: SkillRepository) -> None:
    repo.commit_skill(make_skill(), "[Skills] create weekly-report")
    skill_md = repo.skills_root / "weekly-report" / "SKILL.md"

    # 直接改磁盘不产生变更记录：退出目录并告警。
    original = skill_md.read_text(encoding="utf-8")
    skill_md.write_text(original.replace("周报整理", "手改的名字"), encoding="utf-8")
    assert repo.load_all() == []

    # 内容改回与 Git 一致后自动回到目录；未经仓库提交的改动不会被静默加载。
    skill_md.write_text(original, encoding="utf-8")
    assert [s.skill_id for s in repo.load_all()] == ["weekly-report"]


# ---------------------------------------------------------------- 提交与回滚


def test_failed_commit_restores_files(repo: SkillRepository, monkeypatch) -> None:
    repo.commit_skill(make_skill(), "[Skills] create weekly-report")
    before = (repo.skills_root / "weekly-report" / "SKILL.md").read_bytes()

    def failing_commit(*args: str):
        from server.errors import SkillStoreUnavailableError

        if "commit" in args:
            raise SkillStoreUnavailableError("提交失败")
        return repo._git_raw(*args)

    monkeypatch.setattr(repo, "_git", failing_commit)
    from server.errors import SkillStoreUnavailableError as StoreUnavailable

    with pytest.raises(StoreUnavailable):
        repo.commit_skill(
            make_skill(name="未遂的修改"),
            "[Skills] patch weekly-report",
            {"references/new.md": b"x"},
        )
    assert (repo.skills_root / "weekly-report" / "SKILL.md").read_bytes() == before
    assert not (repo.skills_root / "weekly-report" / "references" / "new.md").exists()
    monkeypatch.undo()
    assert repo.load("weekly-report").name == "周报整理"


# ---------------------------------------------------------------- 版本历史


def test_versions_and_snapshot(repo: SkillRepository) -> None:
    first = make_skill(body="第一版\n")
    repo.commit_skill(
        first, "[Skills] create weekly-report\n\nchange-id: ch-1\nactor: user\nreason: 初版"
    )
    second = make_skill(body="第二版\n")
    repo.commit_skill(
        second,
        "[Skills] patch weekly-report\n\nchange-id: ch-2\nactor: foreground\nreason: 明确学习",
    )
    repo.commit_skill(
        make_skill(body="第二版\n", state=SkillState.ARCHIVED),
        "[Skills] archive weekly-report",
    )

    versions = repo.versions("weekly-report")
    assert len(versions) == 3
    head = versions[0]
    assert (head.change_id, head.actor) == (None, None)  # 归档提交没有变更尾注
    assert versions[1].change_id == "ch-2"
    assert versions[1].actor == "foreground"
    assert versions[1].reason == "明确学习"
    assert versions[2].change_id == "ch-1"

    # 版本的 revision 即内容版本；快照能还原旧正文。
    assert versions[2].skill.body == "第一版\n"
    assert versions[2].revision == first.revision
    assert head.skill.body == "第二版\n"
    assert repo.version_snapshot("weekly-report", versions[2].commit).revision == first.revision

    # 管理策略（归档）不产生新内容版本：归档提交与前一提交的内容 revision 相同。
    assert head.revision == versions[1].revision


def test_state_change_does_not_create_new_revision(repo: SkillRepository) -> None:
    repo.commit_skill(make_skill(), "[Skills] create weekly-report")
    active = repo.load("weekly-report")
    repo.commit_skill(make_skill(state=SkillState.STALE), "[Skills] mark stale")
    stale = repo.load("weekly-report")
    assert stale.state is SkillState.STALE
    assert stale.revision == active.revision


def test_parse_skill_md_rejects_broken_frontmatter() -> None:
    with pytest.raises(ValueError):
        parse_skill_md("没有分隔线")
    with pytest.raises(ValueError):
        parse_skill_md("---\nname: 半个\n")
    with pytest.raises(ValueError):
        parse_skill_md("---\nname: 缺字段\n---\n正文")
    frontmatter, body = parse_skill_md(
        "---\nname: n\ndescription: d\norigin: user\nmanaged: false\nstate: active\n---\n\n正文"
    )
    assert frontmatter["name"] == "n"
    assert body == "正文"
