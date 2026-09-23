"""技能服务层：变更的应用规则矩阵、冲突、审批、目录与加载记录。"""

import pytest

from server.db import init_db
from server.errors import SkillConflictError, SkillUnknownError, SkillValidationError
from server.skills.models import ChangeAction, ChangeActor, SkillOrigin, SkillState
from server.skills.service import ChangeRequest, SkillService


@pytest.fixture
def service(settings) -> SkillService:
    init_db()
    return SkillService(settings.data_dir, settings.db_path)


def create(service: SkillService, skill_id: str = "weekly-report", **payload) -> dict:
    body = {
        "skill_id": skill_id,
        "name": "周报整理",
        "description": "按固定分节整理本周周报",
        "body": "## 步骤\n\n1. 先查日历\n",
    }
    body.update(payload)
    return service.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload=body,
            actor=ChangeActor.USER,
            reason="管理页创建",
        )
    )


# ---------------------------------------------------------------- 来源与应用规则


def test_user_create_applies_immediately(service: SkillService) -> None:
    result = create(service)
    assert result["status"] == "applied"
    skill = service.get("weekly-report")
    assert skill.origin is SkillOrigin.USER
    assert skill.managed is False
    assert skill.state is SkillState.ACTIVE
    # 提交尾注可从版本历史读回。
    version = service.repository.versions("weekly-report")[0]
    assert version.change_id == result["id"]
    assert version.actor == "user"
    assert version.reason == "管理页创建"


def test_explicit_learning_creates_managed_skill(service: SkillService) -> None:
    result = service.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload={
                "skill_id": "screen-cast",
                "name": "投屏排查",
                "description": "排查投屏失败的固定顺序",
                "body": "先查网络，再查分辨率。",
            },
            actor=ChangeActor.FOREGROUND,
            reason="用户要求把刚才的排查记成技能",
        )
    )
    assert result["status"] == "applied"
    skill = service.get("screen-cast")
    assert skill.origin is SkillOrigin.EXPLICIT
    assert skill.managed is True


def test_review_cannot_touch_user_skill_without_approval(service: SkillService) -> None:
    create(service)
    proposed = service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"old_string": "先查日历", "new_string": "先查本周日程"},
            actor=ChangeActor.REVIEW,
            reason="复盘补充",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    assert proposed["status"] == "proposed"
    assert "先查日历" in service.get("weekly-report").body  # 正文未被改动

    approved = service.approve(proposed["id"], expected_revision=proposed["base_revision"])
    assert approved["status"] == "applied"
    assert "先查本周日程" in service.get("weekly-report").body


def test_review_writes_managed_skill_directly(service: SkillService) -> None:
    # 复盘自建的技能默认 managed=true，复盘可以直接更新。
    service.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload={
                "skill_id": "reviewed-flow",
                "name": "复盘沉淀",
                "description": "后台复盘沉淀的做法",
                "body": "旧正文\n",
            },
            actor=ChangeActor.REVIEW,
            reason="复盘沉淀",
        )
    )
    result = service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "新正文\n"},
            actor=ChangeActor.REVIEW,
            reason="复盘修正",
            skill_id="reviewed-flow",
            base_revision=service.get("reviewed-flow").revision,
        )
    )
    assert result["status"] == "applied"
    assert service.get("reviewed-flow").body == "新正文\n"


def test_user_skill_is_never_managed(service: SkillService) -> None:
    """`origin=user` 恒为 managed=false：创建时给不了，管理页也开不了（契约 §2/§5）。"""

    create(service, managed=True)
    assert service.get("weekly-report").managed is False

    with pytest.raises(SkillValidationError) as failure:
        service.set_managed("weekly-report", True)
    assert failure.value.errors[0]["field"] == "managed"
    assert service.get("weekly-report").managed is False

    # 复盘因此只能提出建议。
    proposed = service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "复盘想直接写\n"},
            actor=ChangeActor.REVIEW,
            reason="复盘修正",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    assert proposed["status"] == "proposed"

    # 非用户来源可以关掉直写权，之后复盘同样只能提建议。
    service.set_managed("weekly-report", False)
    assert service.get("weekly-report").managed is False


def test_foreground_may_edit_user_skill_directly(service: SkillService) -> None:
    create(service)
    result = service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "用户当轮要求的新正文\n"},
            actor=ChangeActor.FOREGROUND,
            reason="用户要求修改",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    assert result["status"] == "applied"


# ---------------------------------------------------------------- 冲突与拒绝


def test_stale_base_revision_conflicts(service: SkillService) -> None:
    create(service)
    stale = service.get("weekly-report").revision
    service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "第一版更新\n"},
            actor=ChangeActor.USER,
            reason="第一次修改",
            skill_id="weekly-report",
            base_revision=stale,
        )
    )
    current = service.get("weekly-report").revision
    assert current != stale
    with pytest.raises(SkillConflictError) as error:
        service.record_change(
            ChangeRequest(
                action=ChangeAction.PATCH,
                payload={"body": "基于过期版本的修改\n"},
                actor=ChangeActor.USER,
                reason="并发修改",
                skill_id="weekly-report",
                base_revision=stale,
            )
        )
    assert error.value.current_revision == current
    conflicts = service.changes(status="conflict")
    assert len(conflicts) == 1


def test_approve_with_stale_revision_conflicts(service: SkillService) -> None:
    create(service)
    proposed = service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "待审正文\n"},
            actor=ChangeActor.REVIEW,
            reason="复盘建议",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    # 审批前技能被用户改了：审批必须失败，正文不动。
    service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "用户自己改的\n"},
            actor=ChangeActor.USER,
            reason="用户修改",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    with pytest.raises(SkillConflictError):
        service.approve(proposed["id"], expected_revision=proposed["base_revision"])
    assert service.changes(status="conflict")
    assert service.get("weekly-report").body == "用户自己改的\n"


def test_reject_leaves_body_untouched(service: SkillService) -> None:
    create(service)
    proposed = service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "被拒绝的正文\n"},
            actor=ChangeActor.REVIEW,
            reason="复盘建议",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    rejected = service.reject(proposed["id"])
    assert rejected["status"] == "rejected"
    assert "先查日历" in service.get("weekly-report").body
    with pytest.raises(SkillConflictError):
        service.approve(proposed["id"], expected_revision=proposed["base_revision"])


def test_duplicate_create_conflicts(service: SkillService) -> None:
    create(service)
    with pytest.raises(SkillValidationError):
        create(service)


def test_patch_anchor_must_match_once(service: SkillService) -> None:
    create(service)
    with pytest.raises(SkillValidationError):
        service.record_change(
            ChangeRequest(
                action=ChangeAction.PATCH,
                payload={"old_string": "不存在的串", "new_string": "新"},
                actor=ChangeActor.USER,
                reason="锚点不匹配",
                skill_id="weekly-report",
                base_revision=service.get("weekly-report").revision,
            )
        )


# ---------------------------------------------------------------- 附件动作


def test_attachment_actions(service: SkillService) -> None:
    create(service)
    written = service.record_change(
        ChangeRequest(
            action=ChangeAction.WRITE_FILE,
            payload={"relative_path": "references/notes.md", "content": "# 笔记"},
            actor=ChangeActor.USER,
            reason="补参考资料",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    assert written["status"] == "applied"
    assert [f.relative_path for f in service.get("weekly-report").files] == ["references/notes.md"]

    removed = service.record_change(
        ChangeRequest(
            action=ChangeAction.REMOVE_FILE,
            payload={"relative_path": "references/notes.md"},
            actor=ChangeActor.USER,
            reason="删除参考资料",
            skill_id="weekly-report",
            base_revision=service.get("weekly-report").revision,
        )
    )
    assert removed["status"] == "applied"
    assert service.get("weekly-report").files == ()

    with pytest.raises(SkillValidationError):
        service.record_change(
            ChangeRequest(
                action=ChangeAction.WRITE_FILE,
                payload={"relative_path": "../SKILL.md", "content": "越界"},
                actor=ChangeActor.USER,
                reason="非法路径",
                skill_id="weekly-report",
                base_revision=service.get("weekly-report").revision,
            )
        )


# ---------------------------------------------------------------- 状态、目录与版本


def test_archive_and_restore_without_new_revision(service: SkillService) -> None:
    create(service)
    revision = service.get("weekly-report").revision
    service.archive("weekly-report")
    assert service.get("weekly-report").state is SkillState.ARCHIVED
    assert service.get("weekly-report").revision == revision
    # 归档技能不进目录，也不可被装配；恢复后照常加载。
    assert service.catalog() == []
    with pytest.raises(SkillUnknownError):
        service.validate_selection(["weekly-report"])
    service.restore("weekly-report")
    assert [entry["skill_id"] for entry in service.catalog()] == ["weekly-report"]
    assert service.validate_selection(["weekly-report"])[0].revision == revision
    # 状态切换不产生变更记录。
    assert service.changes(skill_id="weekly-report") and all(
        change["action"] != "state" for change in service.changes(skill_id="weekly-report")
    )


def test_restore_version_creates_new_patch(service: SkillService) -> None:
    create(service)
    first = service.get("weekly-report").revision
    service.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"body": "第二版正文\n"},
            actor=ChangeActor.USER,
            reason="修改",
            skill_id="weekly-report",
            base_revision=first,
        )
    )
    result = service.restore_version("weekly-report", first)
    assert result["status"] == "applied"
    assert "先查日历" in service.get("weekly-report").body
    # 内容回到第一版，内容版本即回到第一版的值；但历史新增了一次提交，不重置历史。
    assert service.get("weekly-report").revision == first
    assert len(service.repository.versions("weekly-report")) == 3


# ---------------------------------------------------------------- 装配与加载记录


def test_selection_limits_and_unknown_skill(service: SkillService) -> None:
    create(service)
    loaded = service.validate_selection(["weekly-report", "weekly-report"])
    assert len(loaded) == 1  # 去重
    with pytest.raises(SkillUnknownError):
        service.validate_selection(["missing-skill"])

    for index in range(11):
        create(service, f"filler-{index:02d}")
    with pytest.raises(SkillValidationError) as over_limit:
        service.validate_selection([f"filler-{index:02d}" for index in range(11)])
    assert over_limit.value.errors[0]["field"] == "skills"

    create(service, "big-skill", body="长" * 30000)
    create(service, "big-skill-2", body="长" * 15000)
    with pytest.raises(SkillValidationError) as over_budget:
        service.validate_selection(["big-skill", "big-skill-2"])
    assert over_budget.value.errors[0]["field"] == "skills"


def test_load_records_and_catalog_order(service: SkillService) -> None:
    create(service, "older")
    create(service, "newer")
    service.record_load("task-1", "run-1", service.get("newer"), "auto")
    entries = service.catalog()
    assert [entry["skill_id"] for entry in entries] == ["newer", "older"]
    assert entries[0]["last_loaded_at"] is not None
    assert entries[1]["last_loaded_at"] is None

    service.record_load("task-1", "run-2", service.get("older"), "manual")
    assert [entry["skill_id"] for entry in service.catalog()] == ["older", "newer"]
    usage = service.task_skill_usage("task-1")
    assert [(row["skill_id"], row["source"]) for row in usage] == [
        ("newer", "auto"),
        ("older", "manual"),
    ]


def test_evidence_items_must_exist(service: SkillService) -> None:
    with pytest.raises(SkillValidationError) as error:
        service.record_change(
            ChangeRequest(
                action=ChangeAction.CREATE,
                payload={
                    "skill_id": "evidence-skill",
                    "name": "名字",
                    "description": "描述",
                    "body": "正文",
                },
                actor=ChangeActor.REVIEW,
                reason="复盘沉淀",
                evidence_item_ids=("item-missing",),
            )
        )
    assert error.value.errors[0]["field"] == "evidence_item_ids"
