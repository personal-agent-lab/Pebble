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
from tests.support import memory_anchor as anchor
from tests.support import seed_memory as seed


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


def test_edit_locates_by_anchor_across_partitions_in_one_call(settings):
    store = MemoryStore(settings.data_dir)
    seed(store, "user", "- 用户默认使用中文\n- 用户偏好先给结论，再解释\n- 内部会议默认 30 分钟")
    seed(store, "memory", "- 工作日历用于内部会议")

    result = store.edit(
        [
            {"action": "move", "anchor": anchor(store, "- 内部会议默认 30 分钟"), "to": "memory"},
            {
                "action": "replace",
                "anchor": anchor(store, "- 用户偏好先给结论，再解释"),
                "text": "用户偏好先给结论",
            },
            {
                "action": "insert",
                "after": anchor(store, "- 用户默认使用中文"),
                "text": "- 代码示例保持完整",
            },
            {
                "action": "insert",
                "after": anchor(store, "- 用户默认使用中文"),
                "text": "- 回答简洁",
            },
        ]
    )

    assert store.snapshot()["user"]["content"] == (
        "- 用户默认使用中文\n- 代码示例保持完整\n- 回答简洁\n- 用户偏好先给结论"
    )
    assert store.snapshot()["memory"]["content"] == "- 工作日历用于内部会议\n- 内部会议默认 30 分钟"
    assert result["applied"][1]["removed"] == ["- 用户偏好先给结论，再解释"]
    assert result["applied"][1]["added"] == ["- 用户偏好先给结论"]
    assert "| - 回答简洁" in result["memory"]["user"]["content"]


def test_duplicates_compare_whole_lines(settings):
    store = MemoryStore(settings.data_dir)
    seed(store, "user", "- 用户偏好简洁回答，但代码要完整")

    added = store.edit([{"action": "append", "target": "user", "text": "用户偏好简洁回答"}])
    repeated = store.edit(
        [
            {"action": "append", "target": "user", "text": "- 用户偏好简洁回答"},
            {"action": "append", "target": "user", "text": "新偏好"},
            {"action": "append", "target": "user", "text": "新偏好"},
        ]
    )

    assert added["changed"] is True
    assert [edit.get("reason") for edit in repeated["applied"]] == ["exists", None, "exists"]
    assert store.snapshot()["user"]["content"] == (
        "- 用户偏好简洁回答，但代码要完整\n\n用户偏好简洁回答\n\n新偏好"
    )


def test_move_into_full_partition_changes_nothing(settings):
    store = MemoryStore(settings.data_dir)
    seed(store, "user", "- 内部会议默认 30 分钟")
    seed(store, "memory", "甲" * 2195)
    before = store.snapshot()

    with pytest.raises(MemoryFullError) as raised:
        store.edit(
            [{"action": "move", "anchor": anchor(store, "- 内部会议默认 30 分钟"), "to": "memory"}]
        )

    assert raised.value.target == "memory" and raised.value.memory is not None
    assert store.snapshot() == before


def test_stale_or_conflicting_anchors_are_rejected_with_current_view(settings):
    store = MemoryStore(settings.data_dir)
    seed(store, "user", "- 内部会议三十分钟\n- 外部会议按邀请")
    stale = anchor(store, "- 内部会议三十分钟")
    other = anchor(store, "- 外部会议按邀请")
    seed(store, "user", "- 内部会议四十五分钟\n- 外部会议按邀请")
    before = store.snapshot()

    with pytest.raises(MemoryValidationError) as raised:
        store.edit([{"action": "delete", "anchor": stale}])
    assert "| - 内部会议四十五分钟" in raised.value.memory["user"]["content"]
    with pytest.raises(MemoryValidationError, match="同一行"):
        store.edit(
            [
                {"action": "delete", "anchor": other},
                {"action": "replace", "anchor": other, "text": "x"},
            ]
        )
    with pytest.raises(MemoryValidationError, match="删除或移走"):
        store.edit(
            [
                {"action": "delete", "anchor": other},
                {"action": "insert", "after": other, "text": "x"},
            ]
        )
    assert store.snapshot() == before


def test_invalid_operations_are_rejected(settings):
    store = MemoryStore(settings.data_dir)

    for operations in (
        [],
        [{"action": "rewrite", "target": "user", "text": "x"}],
        [{"action": "append", "target": "user"}],
        [{"action": "append", "target": "unknown", "text": "x"}],
        [{"action": "append", "target": "user", "text": "k3f9| - 用户默认使用中文"}],
    ):
        with pytest.raises(MemoryValidationError):
            store.edit(operations)
    assert store.snapshot()["user"]["content"] == ""


def test_delete_range_and_append_keeps_list_structure(settings):
    store = MemoryStore(settings.data_dir)
    seed(store, "memory", "## 会议\n\n- 内部会议三十分钟\n- 外部会议按邀请\n- 周五不排会")

    store.edit(
        [
            {
                "action": "delete",
                "anchor": anchor(store, "- 外部会议按邀请"),
                "end_anchor": anchor(store, "- 周五不排会"),
            },
            {"action": "append", "target": "memory", "text": "- 周一上午不排会"},
        ]
    )

    assert (
        store.snapshot()["memory"]["content"] == "## 会议\n\n- 内部会议三十分钟\n- 周一上午不排会"
    )


def test_file_is_read_from_disk_each_time(settings):
    store = MemoryStore(settings.data_dir)
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
    seed(store, target, "甲" * limit)
    before = store.snapshot()[target]

    with pytest.raises(MemoryFullError) as raised:
        store.edit([{"action": "append", "target": target, "text": "乙"}])
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
        store.edit([{"action": "append", "target": "user", "text": "丙"}])
    # 仍然超限但变小的整理可以保存。
    store.edit([{"action": "replace", "anchor": anchor(store, "乙" * 20), "text": "乙"}])
    assert store.snapshot()["user"]["usage"]["chars"] < snapshot["usage"]["chars"]


def test_concurrent_additions_do_not_lose_content(settings):
    store = MemoryStore(settings.data_dir)
    lines = [f"长期偏好 {index}" for index in range(8)]

    def add(line):
        store.edit([{"action": "append", "target": "memory", "text": line}])

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(add, lines))

    assert set(store.snapshot()["memory"]["content"].split("\n\n")) == set(lines)


def test_write_failure_leaves_file_unchanged(settings, monkeypatch):
    store = MemoryStore(settings.data_dir)
    seed(store, "user", "已有内容")
    path = settings.data_dir / "memory" / "USER.md"
    before = path.read_bytes()

    def fail(*_args):
        raise OSError("磁盘已满")

    monkeypatch.setattr(MemoryStore, "_atomic_write", staticmethod(fail))
    with pytest.raises(MemoryStoreUnavailableError):
        store.edit([{"action": "append", "target": "user", "text": "不能留下的内容"}])
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
