"""长期记忆使用真实文件和真实本地 Git；只在临时实例目录中写入。"""

import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.errors import MemoryFullError, MemoryStoreUnavailableError, MemoryValidationError
from server.memory.service import MemoryStore


def git(data_dir, *args):
    return subprocess.run(
        ["git", "-C", str(data_dir), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_initializes_two_files_and_private_history(settings):
    store = MemoryStore(settings.data_dir)

    assert store.snapshot()["user"]["entries"] == []
    assert (settings.data_dir / "memory" / "USER.md").read_text() == ""
    assert (settings.data_dir / "memory" / "MEMORY.md").read_text() == ""
    assert git(settings.data_dir, "config", "user.name") == "Pebble"
    assert git(settings.data_dir, "config", "user.email") == "pebble@local"
    assert git(settings.data_dir, "rev-list", "--count", "HEAD") == "1"
    assert set(git(settings.data_dir, "ls-files").splitlines()) == {
        ".gitignore",
        "memory/MEMORY.md",
        "memory/USER.md",
    }


def test_add_replace_remove_and_duplicate(settings):
    store = MemoryStore(settings.data_dir)
    initial = git(settings.data_dir, "rev-list", "--count", "HEAD")

    added = store.apply("add", "user", "回答先给结论")
    duplicate = store.apply("add", "user", "回答先给结论")
    replaced = store.apply("replace", "user", "回答先解释推导", "先给结论")
    removed = store.apply("remove", "user", old_text="先解释")

    assert added["changed"] is True
    assert duplicate == {**added, "changed": False}
    assert replaced["entries"] == ["回答先解释推导"]
    assert removed["entries"] == []
    assert git(settings.data_dir, "rev-list", "--count", "HEAD") == str(int(initial) + 3)


def test_multiline_entries_are_read_from_disk_each_time(settings):
    store = MemoryStore(settings.data_dir)
    store.apply("add", "memory", "项目使用 Python。\n适用范围：服务端。")
    path = settings.data_dir / "memory" / "MEMORY.md"
    path.write_text("用户直接修改后的事实\n§\n另一条事实", encoding="utf-8")

    assert store.snapshot()["memory"]["entries"] == ["用户直接修改后的事实", "另一条事实"]


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
    assert git(settings.data_dir, "rev-list", "--count", "HEAD") == str(len(entries) + 1)


def test_commit_failure_restores_file_and_index(settings, monkeypatch):
    store = MemoryStore(settings.data_dir)
    path = settings.data_dir / "memory" / "USER.md"
    before = path.read_bytes()

    def fail(*_args):
        raise MemoryStoreUnavailableError("模拟提交失败")

    monkeypatch.setattr(store, "_commit", fail)
    with pytest.raises(MemoryStoreUnavailableError, match="模拟提交失败"):
        store.apply("add", "user", "不能留下的内容")

    assert path.read_bytes() == before
    assert git(settings.data_dir, "status", "--porcelain", "--", "memory/USER.md") == ""


def test_non_content_instance_files_are_never_tracked(settings):
    store = MemoryStore(settings.data_dir)
    (settings.data_dir / "credentials.json").write_text("secret")
    (settings.data_dir / "pebble.db").write_text("database")
    config = settings.data_dir / "agent" / "config"
    config.mkdir(parents=True)
    (config / "session.json").write_text("session")
    store.apply("add", "memory", "只提交这一条记忆")

    tracked = set(git(settings.data_dir, "ls-files").splitlines())
    assert "credentials.json" not in tracked
    assert "pebble.db" not in tracked
    assert "agent/config/session.json" not in tracked
