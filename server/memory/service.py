"""两个 Markdown 文件组成的长期记忆，以及独立的本地 Git 版本历史。"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from server.errors import MemoryFullError, MemoryStoreUnavailableError, MemoryValidationError
from server.storage.datarepo import GITIGNORE
from server.storage.datarepo import lock_for as _lock_for

ENTRY_DELIMITER = "\n\n§\n\n"
TARGETS = {
    "user": ("USER.md", 1375),
    "memory": ("MEMORY.md", 2200),
}
INITIAL_COMMIT = "[Memory] Initialize persistent memory"
COMMIT_ACTIONS = {
    "add": "Add",
    "replace": "Replace",
    "remove": "Remove",
}


class MemoryStore:
    """读写当前有效记忆；每次调用都重新读取磁盘，不缓存用户内容。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.memory_dir = self.data_dir / "memory"
        self._lock = _lock_for(self.data_dir)
        with self._lock:
            self._initialize()

    def snapshot(self) -> dict[str, dict]:
        """返回两个目标的当前内容；直接文件修改会在本次读取中生效。"""
        with self._lock:
            return {target: self._target_snapshot(target) for target in TARGETS}

    def apply(
        self,
        action: str,
        target: str,
        content: str | None = None,
        old_text: str | None = None,
    ) -> dict:
        """新增、替换或删除一个条目，并把一次有效修改提交到本地 Git。"""
        self._validate_request(action, target, content, old_text)
        with self._lock:
            current = self._target_snapshot(target)
            entries = list(current["entries"])
            changed, old = self._change(entries, action, content, old_text)
            serialized = self._serialize(entries)
            limit = TARGETS[target][1]
            if len(serialized) > limit:
                raise MemoryFullError(target, len(serialized), limit)

            if not changed:
                return self._result(target, action, False, entries, self._head(), old)

            path = self._path(target)
            previous = path.read_bytes()
            try:
                self._atomic_write(path, serialized)
                commit = self._commit(path, action, target)
            except Exception as error:
                self._restore(path, previous)
                if isinstance(error, MemoryStoreUnavailableError):
                    raise
                raise MemoryStoreUnavailableError("长期记忆保存失败，文件已恢复") from error
            return self._result(target, action, True, entries, commit, old)

    def _initialize(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_repository()
            ignore = self.data_dir / ".gitignore"
            if not ignore.exists() or ignore.read_text(encoding="utf-8") != GITIGNORE:
                self._atomic_write(ignore, GITIGNORE)
            for target in TARGETS:
                path = self._path(target)
                if not path.exists():
                    self._atomic_write(path, "")
            paths = [ignore, *(self._path(target) for target in TARGETS)]
            self._git("add", "--", *(self._relative(path) for path in paths))
            if self._git_changed(*paths):
                self._git(
                    "commit",
                    "--quiet",
                    "--only",
                    "-m",
                    INITIAL_COMMIT,
                    "--",
                    *(self._relative(path) for path in paths),
                )
        except MemoryStoreUnavailableError:
            raise
        except (OSError, UnicodeError) as error:
            raise MemoryStoreUnavailableError("无法初始化长期记忆文件") from error

    def _ensure_repository(self) -> None:
        if not (self.data_dir / ".git").exists():
            self._git("init", "--quiet")
        root = self._git("rev-parse", "--show-toplevel").stdout.strip()
        if Path(root).resolve() != self.data_dir:
            raise MemoryStoreUnavailableError("实例数据目录不是独立的 Git 仓库")
        self._git("config", "user.name", "Pebble")
        self._git("config", "user.email", "pebble@local")

    def _target_snapshot(self, target: str) -> dict:
        if target not in TARGETS:
            raise MemoryValidationError([{"field": "target", "message": "必须是 user 或 memory"}])
        path = self._path(target)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise MemoryStoreUnavailableError(f"无法读取 {path.name}") from error
        entries = self._parse(content)
        normalized = self._serialize(entries)
        limit = TARGETS[target][1]
        if len(normalized) > limit:
            raise MemoryFullError(target, len(normalized), limit)
        return {
            "target": target,
            "content": normalized,
            "entries": entries,
            "usage": {"chars": len(normalized), "limit": limit},
        }

    @staticmethod
    def _validate_request(
        action: str, target: str, content: str | None, old_text: str | None
    ) -> None:
        errors = []
        if action not in COMMIT_ACTIONS:
            errors.append({"field": "action", "message": "必须是 add、replace 或 remove"})
        if target not in TARGETS:
            errors.append({"field": "target", "message": "必须是 user 或 memory"})
        if action in {"add", "replace"} and not (content or "").strip():
            errors.append({"field": "content", "message": "新增或替换内容不能为空"})
        if action in {"replace", "remove"} and not (old_text or "").strip():
            errors.append({"field": "old_text", "message": "替换或删除必须提供原内容片段"})
        if content is not None and any(line.strip() == "§" for line in content.splitlines()):
            errors.append({"field": "content", "message": "内容不能包含独立的 § 分隔行"})
        if errors:
            raise MemoryValidationError(errors)

    @staticmethod
    def _change(
        entries: list[str], action: str, content: str | None, old_text: str | None
    ) -> tuple[bool, str | None]:
        """返回（是否有有效修改, 被替换或移除的原条目）；新增没有原条目。"""
        normalized = (content or "").strip()
        if action == "add":
            if normalized in entries:
                return False, None
            entries.append(normalized)
            return True, None

        needle = (old_text or "").strip()
        matches = [index for index, entry in enumerate(entries) if needle in entry]
        if len(matches) != 1:
            reason = "没有找到匹配条目" if not matches else "匹配到多个条目，请提供更具体的片段"
            raise MemoryValidationError([{"field": "old_text", "message": reason}])
        index = matches[0]
        old = entries[index]
        if action == "remove":
            entries.pop(index)
            return True, old
        if old == normalized:
            return False, old
        entries.pop(index)
        if normalized not in entries:
            entries.insert(index, normalized)
        return True, old

    @staticmethod
    def _parse(content: str) -> list[str]:
        normalized = content.replace("\r\n", "\n").strip()
        if not normalized:
            return []
        return [
            entry.strip() for entry in re.split(r"(?m)^[ \t]*§[ \t]*$", normalized) if entry.strip()
        ]

    @staticmethod
    def _serialize(entries: list[str]) -> str:
        return ENTRY_DELIMITER.join(entry.strip() for entry in entries if entry.strip())

    def _path(self, target: str) -> Path:
        return self.memory_dir / TARGETS[target][0]

    def _result(
        self,
        target: str,
        action: str,
        changed: bool,
        entries: list[str],
        commit: str,
        old: str | None,
    ) -> dict:
        content = self._serialize(entries)
        return {
            "target": target,
            "action": action,
            "changed": changed,
            "entries": entries,
            "usage": {"chars": len(content), "limit": TARGETS[target][1]},
            "commit": commit,
            "old": old,
        }

    def _commit(self, path: Path, action: str, target: str) -> str:
        relative = self._relative(path)
        self._git("add", "--", relative)
        try:
            self._git(
                "commit",
                "--quiet",
                "--only",
                "-m",
                f"[Memory] {COMMIT_ACTIONS[action]} {target} entry",
                "--",
                relative,
            )
        except MemoryStoreUnavailableError:
            raise
        return self._head()

    def _restore(self, path: Path, previous: bytes) -> None:
        try:
            self._atomic_write_bytes(path, previous)
            self._git("add", "--", self._relative(path))
        except Exception as rollback_error:
            raise MemoryStoreUnavailableError(
                "长期记忆保存失败，且无法恢复原文件"
            ) from rollback_error

    def _git_changed(self, *paths: Path) -> bool:
        result = self._git_raw(
            "diff", "--cached", "--quiet", "--", *(self._relative(path) for path in paths)
        )
        if result.returncode not in {0, 1}:
            raise MemoryStoreUnavailableError("无法检查长期记忆版本状态")
        return result.returncode == 1

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _relative(self, path: Path) -> str:
        return str(path.relative_to(self.data_dir))

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._git_raw(*args)
        if result.returncode != 0:
            raise MemoryStoreUnavailableError("无法更新长期记忆版本历史")
        return result

    def _git_raw(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(self.data_dir), *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise MemoryStoreUnavailableError("本机 Git 不可用") from error

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        MemoryStore._atomic_write_bytes(path, content.encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
