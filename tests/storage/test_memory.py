"""长期记忆使用真实文件；只在临时实例目录中写入。记忆不做版本管理。"""

import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.errors import (
    MemoryFullError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
    VersionConflictError,
)
from server.memory.service import UNTRACK_MESSAGE, MemoryStore
from server.storage.datarepo import GITIGNORE


def git(data_dir, *args):
    return subprocess.run(
        ["git", "-C", str(data_dir), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_initializes_two_empty_files_without_repository(settings):
    store = MemoryStore(settings.data_dir)

    assert store.snapshot()["user"]["content"] == ""
    assert (settings.data_dir / "memory" / "USER.md").read_text() == ""
    assert (settings.data_dir / "memory" / "MEMORY.md").read_text() == ""
    assert not (settings.data_dir / ".git").exists()


def test_edit_appends_replaces_and_removes_fragments(settings):
    store = MemoryStore(settings.data_dir)

    added = store.edit("user", new_text="- 回答先给结论")
    duplicate = store.edit("user", new_text="回答先给结论")
    store.edit("user", new_text="- 默认使用中文")
    replaced = store.edit("user", "先给结论", "先解释推导")
    removed = store.edit("user", "- 默认使用中文")

    assert added["changed"] is True and added["content"] == "- 回答先给结论"
    assert duplicate["changed"] is False and duplicate["version"] == added["version"]
    assert duplicate["applied"] == [{"old_text": "", "new_text": "回答先给结论", "changed": False}]
    assert replaced["content"] == "- 回答先解释推导\n\n- 默认使用中文"
    assert removed["content"] == "- 回答先解释推导"
    assert (settings.data_dir / "memory" / "USER.md").read_text() == "- 回答先解释推导"


def test_batch_edits_apply_in_order_and_check_capacity_on_final_result(settings):
    store = MemoryStore(settings.data_dir)
    store.write("user", "- " + "甲" * 1300 + "\n- 回答先给结论", expected_version=_empty(store))

    # 单独追加会超限；同一批里先精简旧内容再追加，最终结果在上限内即可保存。
    with pytest.raises(MemoryFullError):
        store.edit("user", new_text="- " + "乙" * 80)
    result = store.edit(
        "user",
        operations=[
            {"old_text": "甲" * 1300, "new_text": "甲" * 10},
            {"old_text": "", "new_text": "- " + "乙" * 80},
            {"old_text": "", "new_text": "回答先给结论"},
        ],
    )

    assert result["changed"] is True
    assert [edit["changed"] for edit in result["applied"]] == [True, True, False]
    assert result["content"] == "- " + "甲" * 10 + "\n- 回答先给结论\n\n- " + "乙" * 80


def test_failed_batch_writes_nothing(settings):
    store = MemoryStore(settings.data_dir)
    store.edit("memory", new_text="- 内部会议三十分钟")
    before = (settings.data_dir / "memory" / "MEMORY.md").read_bytes()

    with pytest.raises(MemoryValidationError, match="第 2 处编辑"):
        store.edit(
            "memory",
            operations=[
                {"old_text": "三十分钟", "new_text": "四十五分钟"},
                {"old_text": "不存在的原文", "new_text": ""},
            ],
        )
    with pytest.raises(MemoryFullError):
        store.edit(
            "memory",
            operations=[
                {"old_text": "- 内部会议三十分钟", "new_text": ""},
                {"old_text": "", "new_text": "乙" * 2201},
            ],
        )
    with pytest.raises(MemoryValidationError):
        store.edit("memory", "三十分钟", operations=[{"old_text": "", "new_text": "x"}])
    with pytest.raises(MemoryValidationError):
        store.edit("memory", operations=[])
    assert (settings.data_dir / "memory" / "MEMORY.md").read_bytes() == before


def test_batch_that_cancels_out_reports_no_change(settings):
    store = MemoryStore(settings.data_dir)
    store.edit("user", new_text="回答先给结论")

    result = store.edit(
        "user",
        operations=[
            {"old_text": "先给结论", "new_text": "先解释推导"},
            {"old_text": "先解释推导", "new_text": "先给结论"},
        ],
    )

    assert result["changed"] is False
    assert [edit["changed"] for edit in result["applied"]] == [False, False]


def _empty(store):
    return store.snapshot()["user"]["version"]


def test_edit_inside_a_list_keeps_document_structure(settings):
    store = MemoryStore(settings.data_dir)
    path = settings.data_dir / "memory" / "MEMORY.md"
    path.write_text("## 会议\n\n- 内部会议三十分钟\n- 外部会议按邀请\n\n## 写作\n\n- 先给结论\n")

    store.edit("memory", "- 外部会议按邀请\n", "")
    result = store.edit("memory", "- 内部会议三十分钟", "- 内部会议三十分钟\n- 周五不排会")

    assert result["content"] == (
        "## 会议\n\n- 内部会议三十分钟\n- 周五不排会\n\n## 写作\n\n- 先给结论"
    )


def test_file_is_read_from_disk_each_time(settings):
    store = MemoryStore(settings.data_dir)
    store.edit("memory", new_text="项目使用 Python。")
    path = settings.data_dir / "memory" / "MEMORY.md"
    path.write_text("用户直接修改后的事实\r\n", encoding="utf-8")

    assert store.snapshot()["memory"]["content"] == "用户直接修改后的事实"


def test_legacy_entry_files_become_paragraphs(settings):
    memory_dir = settings.data_dir / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "USER.md").write_text("回答先给结论\n\n§\n\n默认使用中文\n适用：写作", "utf-8")

    store = MemoryStore(settings.data_dir)

    assert store.snapshot()["user"]["content"] == "回答先给结论\n\n默认使用中文\n适用：写作"
    assert "§" not in (memory_dir / "USER.md").read_text(encoding="utf-8")


def test_write_follows_version_and_rejects_stale_writes(settings):
    store = MemoryStore(settings.data_dir)
    empty = store.snapshot()["user"]["version"]
    saved = store.write("user", "# 关于我\n\n回答先给结论\n", expected_version=empty)
    assert saved["changed"] is True and saved["content"] == "# 关于我\n\n回答先给结论"
    assert saved["version"] == store.snapshot()["user"]["version"] != empty

    # 读取之后文件被判断或编辑器改过：带旧版本的写入被拒绝，文件不变。
    (settings.data_dir / "memory" / "USER.md").write_text("编辑器改的", encoding="utf-8")
    with pytest.raises(VersionConflictError) as raised:
        store.write("user", "页面内容", expected_version=saved["version"])
    assert raised.value.current_version == store.snapshot()["user"]["version"]
    assert store.snapshot()["user"]["content"] == "编辑器改的"


@pytest.mark.parametrize(("target", "limit"), [("user", 1375), ("memory", 2200)])
def test_capacity_is_enforced_without_changing_file(settings, target, limit):
    store = MemoryStore(settings.data_dir)
    accepted = store.edit(target, new_text="甲" * limit)
    before = store.snapshot()[target]

    assert accepted["usage"] == {"chars": limit, "limit": limit}
    with pytest.raises(MemoryFullError) as raised:
        store.edit(target, new_text="乙")
    assert raised.value.used > limit
    with pytest.raises(MemoryFullError):
        store.write(target, "甲" * (limit + 1), expected_version=before["version"])
    assert store.snapshot()[target] == before


def test_oversized_file_is_readable_and_can_shrink(settings):
    store = MemoryStore(settings.data_dir)
    path = settings.data_dir / "memory" / "USER.md"
    path.write_text("甲" * 1400 + "\n\n" + "乙" * 20, encoding="utf-8")

    snapshot = store.snapshot()["user"]
    assert snapshot["usage"]["chars"] > snapshot["usage"]["limit"]
    with pytest.raises(MemoryFullError):
        store.edit("user", new_text="丙")
    # 仍然超限但变小的整理可以保存。
    shrunk = store.edit("user", "乙" * 20, "乙")
    assert shrunk["changed"] is True and shrunk["usage"]["chars"] < snapshot["usage"]["chars"]


def test_invalid_or_ambiguous_edit_does_not_write(settings):
    store = MemoryStore(settings.data_dir)
    store.edit("user", new_text="内部会议默认三十分钟")
    store.edit("user", new_text="外部会议按邀请时长")
    before = (settings.data_dir / "memory" / "USER.md").read_bytes()

    with pytest.raises(MemoryValidationError, match="没有找到"):
        store.edit("user", "不存在")
    with pytest.raises(MemoryValidationError, match="多次"):
        store.edit("user", "会议", "会议稍后决定")
    with pytest.raises(MemoryValidationError):
        store.edit("user", "", "  ")
    with pytest.raises(MemoryValidationError):
        store.edit("unknown", new_text="内容")
    assert (settings.data_dir / "memory" / "USER.md").read_bytes() == before


def test_concurrent_additions_do_not_lose_content(settings):
    store = MemoryStore(settings.data_dir)
    lines = [f"长期偏好 {index}" for index in range(8)]

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda line: store.edit("memory", new_text=line), lines))

    assert set(store.snapshot()["memory"]["content"].split("\n\n")) == set(lines)


def test_write_failure_leaves_file_unchanged(settings, monkeypatch):
    store = MemoryStore(settings.data_dir)
    store.edit("user", new_text="已有内容")
    path = settings.data_dir / "memory" / "USER.md"
    before = path.read_bytes()

    def fail(*_args):
        raise OSError("磁盘已满")

    monkeypatch.setattr(MemoryStore, "_atomic_write", staticmethod(fail))
    with pytest.raises(MemoryStoreUnavailableError):
        store.edit("user", new_text="不能留下的内容")
    assert path.read_bytes() == before


def test_previously_versioned_memory_is_untracked_once(settings):
    data_dir = settings.data_dir
    (data_dir / "memory").mkdir(parents=True)
    (data_dir / "kb").mkdir()
    (data_dir / "memory" / "USER.md").write_text("旧实例里的记忆", encoding="utf-8")
    (data_dir / "kb" / "a.md").write_text("资料", encoding="utf-8")
    (data_dir / ".gitignore").write_text(
        "*\n!.gitignore\n!memory/\n!memory/**\n!kb/\n!kb/**\n", encoding="utf-8"
    )
    git(data_dir, "init", "--quiet")
    git(data_dir, "config", "user.name", "Pebble")
    git(data_dir, "config", "user.email", "pebble@local")
    git(data_dir, "add", "--all")
    git(data_dir, "commit", "--quiet", "-m", "旧实例")
    # 资料库已暂存、尚未提交的改动不能被一并提交。
    (data_dir / "kb" / "a.md").write_text("资料改过", encoding="utf-8")
    git(data_dir, "add", "--", "kb")

    store = MemoryStore(data_dir)
    MemoryStore(data_dir)  # 第二次构造不再产生提交

    assert store.snapshot()["user"]["content"] == "旧实例里的记忆"
    assert git(data_dir, "log", "--format=%s").splitlines() == [UNTRACK_MESSAGE, "旧实例"]
    assert git(data_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        ".gitignore",
        "kb/a.md",
    ]
    assert git(data_dir, "show", "HEAD:.gitignore") + "\n" == GITIGNORE
    assert git(data_dir, "status", "--porcelain") == "M  kb/a.md"
