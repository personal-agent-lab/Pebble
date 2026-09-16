"""资料库使用真实文件和真实本地 Git；只在临时实例目录中写入。"""

import subprocess

import pytest

from server.agent.toolset import (
    ALLOWED_EFFECTS,
    ToolDeps,
    TurnKind,
    build_tools,
    exposed_tools,
)
from server.errors import (
    KbStoreUnavailableError,
    KbValidationError,
    NotFoundError,
    VersionConflictError,
)
from server.tools.personal_kb.service import KbStore
from server.tools.registry import default_registry


def git(data_dir, *args):
    return subprocess.run(
        ["git", "-C", str(data_dir), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_save_writes_markdown_file_with_frontmatter_and_commits(settings):
    store = KbStore(settings.data_dir)

    saved = store.save(
        title="GSE lab1 要求",
        body="## 概要\n第一次实验的要求。",
        tags=["课程", "GSE"],
        source={"kind": "user"},
    )

    path = settings.data_dir / saved["path"]
    assert saved["path"].startswith("kb/inbox/") and saved["path"].endswith(".md")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert f"id: {saved['id']}" in text
    assert "title: GSE lab1 要求" in text
    assert "第一次实验的要求" in text
    # 版本即 git 提交
    assert saved["version"] == git(settings.data_dir, "rev-parse", "HEAD")
    assert saved["ref"]["commit"] == saved["version"]
    assert "[Kb] Add" in git(settings.data_dir, "log", "--oneline")


def test_save_honours_custom_relative_path_and_appends_extension(settings):
    store = KbStore(settings.data_dir)

    saved = store.save(title="张老师", body="沟通偏好：先结论。", path="课程/张老师")

    assert saved["path"] == "kb/课程/张老师.md"
    assert (settings.data_dir / "kb" / "课程" / "张老师.md").exists()


def test_read_returns_original_body_by_id_and_by_path(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="报告大纲", body="## 第一节\n原文内容，不被总结。")

    by_id = store.read(doc_id=saved["id"])
    by_path = store.read(path=saved["path"])

    assert by_id["title"] == "报告大纲"
    assert "原文内容，不被总结" in by_id["body"]
    assert by_path["id"] == saved["id"]


def test_update_keeps_identity_and_modifies_same_file(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="清单", body="原始正文。")
    files_before = set(git(settings.data_dir, "ls-files").splitlines())

    updated = store.update(
        doc_id=saved["id"], expected_version=saved["version"], body="修改后的正文。"
    )

    assert updated["id"] == saved["id"]
    assert updated["path"] == saved["path"]
    assert updated["version"] != saved["version"]
    # 不新建副本：跟踪文件集合不变
    assert set(git(settings.data_dir, "ls-files").splitlines()) == files_before
    assert "修改后的正文" in store.read(doc_id=saved["id"])["body"]


def test_read_historical_version_returns_old_content(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="草稿", body="第一版内容。")
    store.update(doc_id=saved["id"], expected_version=saved["version"], body="第二版内容。")

    historical = store.read(doc_id=saved["id"], version=saved["version"])
    current = store.read(doc_id=saved["id"])

    assert "第一版内容" in historical["body"]
    assert "第二版内容" in current["body"]


def test_update_with_stale_version_is_rejected_without_writing(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="并发资料", body="初始正文。")
    store.update(doc_id=saved["id"], expected_version=saved["version"], body="已更新的正文。")
    path = settings.data_dir / saved["path"]
    before = path.read_bytes()

    with pytest.raises(VersionConflictError):
        store.update(doc_id=saved["id"], expected_version=saved["version"], body="迟到的覆盖。")

    assert path.read_bytes() == before
    assert "已更新的正文" in store.read(doc_id=saved["id"])["body"]


def test_save_rejects_existing_path_empty_fields_and_unsafe_paths(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="已有资料", body="正文。")

    with pytest.raises(KbValidationError, match="已存在"):
        store.save(title="重复", body="正文。", path=saved["path"])
    with pytest.raises(KbValidationError):
        store.save(title="", body="正文。")
    with pytest.raises(KbValidationError):
        store.save(title="标题", body="   ")
    with pytest.raises(KbValidationError):
        store.save(title="越界", body="正文。", path="../evil.md")
    with pytest.raises(KbValidationError):
        store.save(title="绝对", body="正文。", path="/etc/evil.md")


def test_read_missing_resource_raises_not_found(settings):
    store = KbStore(settings.data_dir)

    with pytest.raises(NotFoundError):
        store.read(doc_id="kb_doesnotexist")
    with pytest.raises(NotFoundError):
        store.read(path="kb/inbox/missing.md")


def test_update_commit_failure_restores_previous_file(settings, monkeypatch):
    store = KbStore(settings.data_dir)
    saved = store.save(title="可恢复", body="原始正文。")
    path = settings.data_dir / saved["path"]
    before = path.read_bytes()

    def fail(*_args):
        raise KbStoreUnavailableError("模拟提交失败")

    monkeypatch.setattr(store, "_commit", fail)
    with pytest.raises(KbStoreUnavailableError, match="模拟提交失败"):
        store.update(doc_id=saved["id"], expected_version=saved["version"], body="写坏的内容。")

    assert path.read_bytes() == before


def test_non_content_instance_files_are_never_tracked(settings):
    store = KbStore(settings.data_dir)
    (settings.data_dir / "pebble.db").write_text("database")
    (settings.data_dir / "credentials.json").write_text("secret")

    store.save(title="只提交这份资料", body="正文。")

    tracked = set(git(settings.data_dir, "ls-files").splitlines())
    assert "pebble.db" not in tracked
    assert "credentials.json" not in tracked
    # 非 ASCII 文件名会被 git 八进制转义并加引号，这里用子串判断
    assert any("kb/" in name for name in tracked)


def test_kb_tools_are_registered_with_correct_schema_and_turn_exposure(settings):
    kb_save = default_registry.get_tool("kb_save")
    props = kb_save.parameters_schema["properties"]
    # 可选数组/对象参数生成正确的 JSON 类型（剥离 Optional 后映射）
    assert props["tags"]["type"] == "array"
    assert props["tags"]["items"] == {"type": "string"}
    assert props["source"]["type"] == "object"
    assert kb_save.parameters_schema["required"] == ["title", "body"]

    store = KbStore(settings.data_dir)
    tools = build_tools(ToolDeps(drafts=None, tasks=None, gmail=None, kb_store=store))
    kb_names = {t.name for t in tools if t.name.startswith("kb_")}
    assert kb_names == {"kb_save", "kb_read", "kb_update"}

    on_message = {
        t.name
        for t in exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
        if t.name.startswith("kb_")
    }
    on_new_mail = {
        t.name
        for t in exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.NEW_MAIL])
        if t.name.startswith("kb_")
    }
    # 写工具只在用户发起的轮次可见，触发轮只读
    assert on_message == {"kb_save", "kb_read", "kb_update"}
    assert on_new_mail == {"kb_read"}


def test_kb_tools_are_skipped_when_store_unavailable(settings):
    tools = build_tools(ToolDeps(drafts=None, tasks=None, gmail=None))
    assert not [t for t in tools if t.name.startswith("kb_")]
