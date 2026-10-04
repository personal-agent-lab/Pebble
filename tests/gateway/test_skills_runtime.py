"""技能装配与工具：目录材料、手动装配、范围强制与工具可见性。"""

import pytest

from server.agent.toolset import ALLOWED_EFFECTS, TurnKind
from server.db import init_db
from server.errors import SkillUnknownError
from server.skills.models import ChangeAction, ChangeActor, SkillState
from server.skills.runtime import (
    SKIPPED_TITLE,
    catalog_material,
    manual_materials,
    turn_scope,
)
from server.skills.service import CATALOG_LIMIT, ChangeRequest, SkillService
from server.skills.tools import skill_list, skill_manage, skill_view
from server.tools.registry import SideEffect, default_registry


@pytest.fixture
def service(settings) -> SkillService:
    init_db()
    return SkillService(settings.data_dir, settings.db_path)


def add_skill(service: SkillService, skill_id: str, **payload) -> None:
    body = {
        "skill_id": skill_id,
        "name": payload.pop("name", f"技能 {skill_id}"),
        "description": payload.pop("description", "一句描述"),
        "body": payload.pop("body", "正文内容\n"),
    }
    body.update(payload)
    service.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE, payload=body, actor=ChangeActor.USER, reason="测试创建"
        )
    )


# ---------------------------------------------------------------- 目录材料


def test_catalog_material_shape_and_limits(service: SkillService) -> None:
    assert catalog_material(service) is None  # 没有技能不注入目录
    for index in range(CATALOG_LIMIT + 5):
        add_skill(service, f"skill-{index:02d}")
    material = catalog_material(service)
    assert material is not None and material.title == "技能目录"
    entries = material.content["skills"]
    assert len(entries) == CATALOG_LIMIT
    assert all({"skill_id", "name", "description"} == set(entry) for entry in entries)
    assert "省略" in material.content
    assert material.content["省略"] == "还有 5 个较久未使用的技能未列出"

    # 描述在创建时已限 160 字符，目录按上限原样带出。
    add_skill(service, "long-desc", description="长" * 160)
    material = catalog_material(service, excluded_skill_ids={"long-desc"})
    # 排除项不进目录。
    assert all(entry["skill_id"] != "long-desc" for entry in material.content["skills"])


# ---------------------------------------------------------------- 手动装配


def test_manual_materials_bind_revision_and_record_load(service: SkillService) -> None:
    add_skill(service, "picked", body="被选中的正文\n")
    materials = manual_materials(
        service, ["picked", "picked", "missing"], task_id="task-1", run_id="run-1"
    )
    assert len(materials) == 2  # 装配 1 份 + 不存在的技能进"未装配"材料
    assert materials[0].title == "用户选择的技能：技能 picked"
    assert materials[0].content == "被选中的正文\n"
    assert materials[1].title == SKIPPED_TITLE
    assert materials[1].content["skills"] == [
        {"skill_id": "missing", "名称": "missing", "原因": "文件有未收编的直接修改或已不可加载"}
    ]
    usage = service.task_skill_usage("task-1")
    assert [(row["skill_id"], row["source"], row["revision"]) for row in usage] == [
        ("picked", "manual", service.get("picked").revision)
    ]


def test_manual_materials_skip_inactive(service: SkillService) -> None:
    add_skill(service, "archived-one")
    service.archive("archived-one")
    materials = manual_materials(service, ["archived-one"], task_id="t", run_id="r")
    assert [material.title for material in materials] == [SKIPPED_TITLE]


# ---------------------------------------------------------------- skill_view 范围强制


def test_skill_view_enforces_exclusion_and_auto_match(service: SkillService) -> None:
    add_skill(service, "excluded-one")
    add_skill(service, "manual-one")
    add_skill(service, "free-one")

    with (
        turn_scope(task_id="t", run_id="r", excluded_skill_ids={"excluded-one"}),
        pytest.raises(SkillUnknownError),
    ):
        skill_view(skill_id="excluded-one", skills=service)

    with turn_scope(task_id="t", run_id="r", auto_match=False, skills=("manual-one",)):
        with pytest.raises(SkillUnknownError):
            skill_view(skill_id="free-one", skills=service)
        # 手动选择的仍可读取。
        result = skill_view(skill_id="manual-one", skills=service)
        assert result["body"] == "正文内容\n"

    # 无范围（例如一次性后台会话）：照常读取，不记加载。
    result = skill_view(skill_id="free-one", skills=service)
    assert result["revision"] == service.get("free-one").revision
    assert service.task_skill_usage("t") == [] or all(
        row["skill_id"] != "free-one" for row in service.task_skill_usage("t")
    )


def test_skill_view_records_auto_load_and_reads_attachment(service: SkillService) -> None:
    add_skill(service, "with-file")
    service.record_change(
        ChangeRequest(
            action=ChangeAction.WRITE_FILE,
            payload={"relative_path": "references/notes.md", "content": "# 附件"},
            actor=ChangeActor.USER,
            reason="补附件",
            skill_id="with-file",
            base_revision=service.get("with-file").revision,
        )
    )
    with turn_scope(task_id="task-9", run_id="run-9"):
        result = skill_view(skill_id="with-file", file_path="references/notes.md", skills=service)
        assert result["file"] == "# 附件"
        assert [f["relative_path"] for f in result["files"]] == ["references/notes.md"]
        assert all(f["content_hash"] for f in result["files"])
    usage = service.task_skill_usage("task-9")
    assert [(row["skill_id"], row["source"]) for row in usage] == [("with-file", "auto")]


def test_skill_list_enforces_exclusion_and_auto_match(service: SkillService) -> None:
    add_skill(service, "excluded-one")
    add_skill(service, "manual-one")

    with turn_scope(task_id="t", run_id="r", excluded_skill_ids={"excluded-one"}):
        listed = [entry["skill_id"] for entry in skill_list(skills=service)]
    assert listed == ["manual-one"]

    # 关闭自动匹配后目录只剩手动选择的那些。
    with turn_scope(task_id="t", run_id="r", auto_match=False, skills=("manual-one",)):
        assert [entry["skill_id"] for entry in skill_list(skills=service)] == ["manual-one"]
    with turn_scope(task_id="t", run_id="r", auto_match=False):
        assert skill_list(skills=service) == []


def test_manual_materials_enforce_selection_limits(service: SkillService) -> None:
    for index in range(12):
        add_skill(service, f"picked-{index:02d}")
    materials = manual_materials(
        service,
        [f"picked-{index:02d}" for index in range(12)],
        task_id="task-1",
        run_id="run-1",
    )
    # 额度在装配层同样成立：10 份正文 + 1 份"未装配"材料说明超限的两个。
    assert len(materials) == 11
    assert materials[-1].title == SKIPPED_TITLE
    assert [entry["skill_id"] for entry in materials[-1].content["skills"]] == [
        "picked-10",
        "picked-11",
    ]

    add_skill(service, "huge", body="长" * 30000)
    add_skill(service, "huge-2", body="长" * 20000)
    capped = manual_materials(service, ["huge", "huge-2"], task_id="task-2", run_id="run-2")
    assert [material.title for material in capped] == ["用户选择的技能：技能 huge", SKIPPED_TITLE]
    assert capped[-1].content["skills"][0]["原因"] == "正文合计超出 40000 字符的装配预算"


def test_manual_materials_skip_notifies_user(service: SkillService) -> None:
    """跳过必须有程序写的用户告知与模型可见材料（skills.md §9），不依赖模型转述。"""

    add_skill(service, "hand-touched")
    path = service.repository.skills_root / "hand-touched" / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("正文内容", "手改的正文"), encoding="utf-8"
    )
    notices: list[str] = []
    materials = manual_materials(
        service, ["hand-touched"], task_id="t", run_id="r", notify=notices.append
    )
    assert [material.title for material in materials] == [SKIPPED_TITLE]
    assert materials[0].content["skills"][0]["名称"] == "技能 hand-touched"
    assert notices == [
        "你选择的技能未能装配：《技能 hand-touched》"
        "（文件有未收编的直接修改或已不可加载）。本轮回复不包含它们的内容。"
    ]


def test_dirty_skill_is_not_loadable(service: SkillService) -> None:
    """直接改磁盘的技能退出目录与装配，不静默加载（契约 §8）。"""

    add_skill(service, "hand-touched")
    add_skill(service, "clean-one")
    path = service.repository.skills_root / "hand-touched" / "SKILL.md"
    edited = path.read_text(encoding="utf-8").replace("正文内容", "手改的正文")
    path.write_text(edited, encoding="utf-8")

    assert [entry["skill_id"] for entry in skill_list(skills=service)] == ["clean-one"]
    with pytest.raises(SkillUnknownError):
        skill_view(skill_id="hand-touched", skills=service)
    with pytest.raises(SkillUnknownError):
        service.validate_selection(["hand-touched"])
    # 不装配正文，但降级告知照常：一份"未装配"材料 + 程序写的时间线通知。
    notices: list[str] = []
    materials = manual_materials(
        service, ["hand-touched"], task_id="t", run_id="r", notify=notices.append
    )
    assert [material.title for material in materials] == [SKIPPED_TITLE]
    assert "技能 hand-touched" in notices[0]
    # 管理页仍可读到它，便于就地修改后重新提交。
    assert "手改的正文" in service.get("hand-touched").body


def test_skill_view_rejects_non_active(service: SkillService) -> None:
    add_skill(service, "staled")
    service._set_state("staled", SkillState.STALE)
    with pytest.raises(SkillUnknownError):
        skill_view(skill_id="staled", skills=service)


def test_skill_list_filters_state(service: SkillService) -> None:
    add_skill(service, "live")
    add_skill(service, "staled")
    service._set_state("staled", SkillState.STALE)
    assert [entry["skill_id"] for entry in skill_list(skills=service)] == ["live"]
    assert {entry["skill_id"] for entry in skill_list(state="stale", skills=service)} == {"staled"}


# ---------------------------------------------------------------- skill_manage 与可见性


def test_skill_manage_records_foreground_change(service: SkillService) -> None:
    result = skill_manage(
        action="create",
        payload={
            "skill_id": "explicit-skill",
            "name": "投屏排查",
            "description": "排查投屏失败的固定顺序",
            "body": "先查网络。",
        },
        reason="用户要求记录刚才的排查",
        skills=service,
    )
    assert result["status"] == "applied"
    skill = service.get("explicit-skill")
    assert skill.managed is True
    assert skill.origin.value == "explicit"


def test_skill_tool_visibility_per_turn_kind() -> None:
    definitions = {definition.name: definition for definition in default_registry.list_tools()}
    assert {"skill_list", "skill_view", "skill_manage"} <= set(definitions)
    assert definitions["skill_manage"].side_effect is SideEffect.LOCAL_WRITE_USER_TURN
    assert definitions["skill_view"].side_effect is SideEffect.READONLY

    for kind in TurnKind:
        visible = {
            definition.name
            for definition in definitions.values()
            if definition.side_effect in ALLOWED_EFFECTS[kind]
        }
        assert {"skill_list", "skill_view"} <= visible
        if kind is TurnKind.MESSAGE:
            assert "skill_manage" in visible
        else:
            assert "skill_manage" not in visible
