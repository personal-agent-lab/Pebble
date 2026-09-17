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

    assert store.snapshot()["user"]["entries"] == []
    assert (settings.data_dir / "memory" / "USER.md").read_text() == ""
    assert (settings.data_dir / "memory" / "MEMORY.md").read_text() == ""
    assert not (settings.data_dir / ".git").exists()


def test_add_replace_remove_and_duplicate(settings):
    store = MemoryStore(settings.data_dir)

    added = store.apply("add", "user", "回答先给结论")
    duplicate = store.apply("add", "user", "回答先给结论")
    replaced = store.apply("replace", "user", "回答先解释推导", "先给结论")
    removed = store.apply("remove", "user", old_text="先解释")

    assert added["changed"] is True
    assert duplicate == {**added, "changed": False}
    assert replaced["entries"] == ["回答先解释推导"] and replaced["old"] == "回答先给结论"
    assert removed["entries"] == [] and removed["old"] == "回答先解释推导"
    assert (settings.data_dir / "memory" / "USER.md").read_text() == ""


def test_multiline_entries_are_read_from_disk_each_time(settings):
    store = MemoryStore(settings.data_dir)
    store.apply("add", "memory", "项目使用 Python。\n适用范围：服务端。")
    path = settings.data_dir / "memory" / "MEMORY.md"
    path.write_text("用户直接修改后的事实\n§\n另一条事实", encoding="utf-8")

    assert store.snapshot()["memory"]["entries"] == ["用户直接修改后的事实", "另一条事实"]


def test_version_follows_content_and_rejects_stale_writes(settings):
    store = MemoryStore(settings.data_dir)
    empty = store.snapshot()["user"]["version"]
    added = store.apply("add", "user", "回答先给结论", expected_version=empty)
    assert added["version"] == store.snapshot()["user"]["version"] != empty

    # 读取之后文件被判断或编辑器改过：带旧版本的写入被拒绝，文件不变。
    (settings.data_dir / "memory" / "USER.md").write_text("编辑器改的", encoding="utf-8")
    with pytest.raises(VersionConflictError) as raised:
        store.apply("add", "user", "页面新增", expected_version=added["version"])
    assert raised.value.current_version == store.snapshot()["user"]["version"]
    assert store.snapshot()["user"]["entries"] == ["编辑器改的"]


def test_exact_match_requires_whole_entry(settings):
    store = MemoryStore(settings.data_dir)
    store.apply("add", "user", "内部会议默认三十分钟")

    with pytest.raises(MemoryValidationError, match="没有找到"):
        store.apply("remove", "user", old_text="内部会议", exact=True)
    result = store.apply(
        "replace", "user", "内部会议默认四十五分钟", "内部会议默认三十分钟", exact=True
    )
    assert result["entries"] == ["内部会议默认四十五分钟"]


@pytest.mark.parametrize(("target", "limit"), [("user", 1375), ("memory", 2200)])
def test_capacity_is_enforced_without_changing_file(settings, target, limit):
    store = MemoryStore(settings.data_dir)
    accepted = store.apply("add", target, "甲" * limit)
    before = store.snapshot()[target]

    assert accepted["usage"] == {"chars": limit, "limit": limit}
    with pytest.raises(MemoryFullError) as raised:
        store.apply("add", target, "乙")
    assert raised.value.used > limit
    assert store.snapshot()[target] == before


def test_oversized_file_is_readable_and_can_shrink(settings):
    store = MemoryStore(settings.data_dir)
    path = settings.data_dir / "memory" / "USER.md"
    path.write_text("甲" * 1400 + "\n§\n" + "乙" * 20, encoding="utf-8")

    snapshot = store.snapshot()["user"]
    assert snapshot["usage"]["chars"] > snapshot["usage"]["limit"]
    with pytest.raises(MemoryFullError):
        store.apply("add", "user", "丙")
    # 仍然超限但变小的整理可以保存。
    shrunk = store.apply("replace", "user", "甲" * 1390, "甲甲甲")
    assert shrunk["changed"] is True and shrunk["usage"]["chars"] < snapshot["usage"]["chars"]


def test_invalid_or_ambiguous_match_does_not_write(settings):
    store = MemoryStore(settings.data_dir)
    store.apply("add", "user", "内部会议默认三十分钟")
    store.apply("add", "user", "外部会议按邀请时长")
    before = (settings.data_dir / "memory" / "USER.md").read_bytes()

    with pytest.raises(MemoryValidationError, match="没有找到"):
        store.apply("remove", "user", old_text="不存在")
    with pytest.raises(MemoryValidationError, match="匹配到多个"):
        store.apply("replace", "user", "会议稍后决定", old_text="会议")
    with pytest.raises(MemoryValidationError):
        store.apply("add", "unknown", "内容")
    assert (settings.data_dir / "memory" / "USER.md").read_bytes() == before


def test_concurrent_additions_do_not_lose_entries(settings):
    store = MemoryStore(settings.data_dir)
    entries = [f"长期偏好 {index}" for index in range(8)]

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda entry: store.apply("add", "memory", entry), entries))

    assert set(store.snapshot()["memory"]["entries"]) == set(entries)


def test_write_failure_leaves_file_unchanged(settings, monkeypatch):
    store = MemoryStore(settings.data_dir)
    store.apply("add", "user", "已有条目")
    path = settings.data_dir / "memory" / "USER.md"
    before = path.read_bytes()

    def fail(*_args):
        raise OSError("磁盘已满")

    monkeypatch.setattr(MemoryStore, "_atomic_write", staticmethod(fail))
    with pytest.raises(MemoryStoreUnavailableError):
        store.apply("add", "user", "不能留下的内容")
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

    assert store.snapshot()["user"]["entries"] == ["旧实例里的记忆"]
    assert git(data_dir, "log", "--format=%s").splitlines() == [UNTRACK_MESSAGE, "旧实例"]
    assert git(data_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        ".gitignore",
        "kb/a.md",
    ]
    assert git(data_dir, "show", "HEAD:.gitignore") + "\n" == GITIGNORE
    assert git(data_dir, "status", "--porcelain") == "M  kb/a.md"
