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


def test_read_lines_cover_the_body_and_whole_body_ref_reads_back_the_body(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="多分节", body="## 甲\n\n第一段。\n\n## 乙\n\n第二段。")
    text_lines = (settings.data_dir / saved["path"]).read_text(encoding="utf-8").splitlines()

    whole = store.read(doc_id=saved["id"])

    # 整篇读取的行号覆盖正文区间（不含 frontmatter），与返回正文严格对应
    assert whole["lines"] == [text_lines.index("## 甲") + 1, len(text_lines)]
    assert saved["ref"]["lines"] == whole["lines"]

    ref = store.search(query="第二段")["results"][0]["ref"]
    fragment = store.read(ref={**ref, "lines": whole["lines"]})
    assert fragment["body"] == whole["body"]


def test_list_discovers_documents_without_knowing_path_or_id(settings):
    store = KbStore(settings.data_dir)
    first = store.save(title="验收会议纪要", body="第一份。")
    store.save(title="课程资料", body="第二份。", path="课程/资料.md")

    all_documents = store.list()["documents"]
    course_documents = store.list(directory="课程")["documents"]

    assert {item["title"] for item in all_documents} == {"验收会议纪要", "课程资料"}
    found = next(item for item in all_documents if item["id"] == first["id"])
    assert found["path"] == first["path"]
    assert [item["title"] for item in course_documents] == ["课程资料"]


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


def test_history_lists_versions_for_conversation_readback(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="草稿", body="第一版内容。")
    updated = store.update(
        doc_id=saved["id"], expected_version=saved["version"], body="第二版内容。"
    )

    history = store.history(doc_id=saved["id"])

    assert history["path"] == saved["path"]
    assert [item["version"] for item in history["versions"]] == [
        updated["version"],
        saved["version"],
    ]
    assert updated["previous_version"] == saved["version"]


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
    # 资料不记录出处：保存与检索都不接受来源参数
    assert "source" not in props
    assert kb_save.parameters_schema["required"] == ["title", "body"]

    kb_search = default_registry.get_tool("kb_search")
    search_props = kb_search.parameters_schema["properties"]
    assert kb_search.parameters_schema["required"] == ["query"]
    assert search_props["max_results"] == {"type": "integer", "default": None}
    assert "source_kind" not in search_props
    assert search_props["tag"]["type"] == "string"

    kb_read = default_registry.get_tool("kb_read")
    ref_prop = kb_read.parameters_schema["properties"]["ref"]
    # 嵌套结构由工具声明必填字段，自动映射只给最粗的 object 类型
    assert ref_prop["type"] == "object"
    assert ref_prop["required"] == ["path", "commit", "lines"]
    assert ref_prop["properties"]["lines"] == {"type": "array", "items": {"type": "integer"}}

    store = KbStore(settings.data_dir)
    tools = build_tools(ToolDeps(drafts=None, tasks=None, gmail=None, kb_store=store))
    visible = {
        kind: {
            t.name
            for t in exposed_tools(tools, allowed=ALLOWED_EFFECTS[kind])
            if t.name.startswith("kb_")
        }
        for kind in TurnKind
    }
    readonly = {"kb_list", "kb_read", "kb_history", "kb_search"}
    everyday = readonly | {"kb_save", "kb_update"}
    destructive = {"kb_delete", "kb_move", "kb_restore"}
    # 删除、移动与恢复只在用户亲自发起的轮次可见；新建与修改在触发轮与回传轮同样可用；
    # 归档不在触发轮出现，执行结果回传轮正是归档的时机
    assert visible[TurnKind.MESSAGE] == everyday | destructive | {"kb_archive"}
    assert visible[TurnKind.NEW_MAIL] == everyday
    assert visible[TurnKind.EXECUTION_RESULT] == everyday | {"kb_archive"}


def test_kb_tools_are_skipped_when_store_unavailable(settings):
    tools = build_tools(ToolDeps(drafts=None, tasks=None, gmail=None))
    assert not [t for t in tools if t.name.startswith("kb_")]


def _manually_edit(path, old, new):
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )


def test_manual_edit_then_save_another_searches_the_edited_content(settings):
    store = KbStore(settings.data_dir)
    store.save(title="alpha", body="## sec\nCORAL-7421")
    target = next((settings.data_dir / "kb" / "inbox").glob("*.md"))
    _manually_edit(target, "CORAL-7421", "REVISED-9000")

    store.save(title="beta", body="无关正文。")

    assert not store.search(query="CORAL-7421")["results"]
    assert store.search(query="REVISED-9000")["results"]


def test_manual_edit_then_search_does_not_return_stale_hits(settings):
    store = KbStore(settings.data_dir)
    store.save(title="alpha", body="## sec\nCORAL-7421")
    target = next((settings.data_dir / "kb" / "inbox").glob("*.md"))
    _manually_edit(target, "CORAL-7421", "REVISED-9000")

    assert not store.search(query="CORAL-7421")["results"]
    assert store.search(query="REVISED-9000")["results"]
