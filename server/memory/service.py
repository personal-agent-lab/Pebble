"""两个 Markdown 文件组成的长期记忆。

记忆不做版本管理：条目简短、由模型持续整理，变化本身已经体现在对话里的记忆提示中。
文件只保存当前有效内容；写入是原子替换，版本号取文件内容的哈希，供管理页做冲突检查。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from server.errors import (
    MemoryFullError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
    VersionConflictError,
)
from server.storage.datarepo import GITIGNORE
from server.storage.datarepo import lock_for as _lock_for

ENTRY_DELIMITER = "\n\n§\n\n"
TARGETS = {
    "user": ("USER.md", 1375),
    "memory": ("MEMORY.md", 2200),
}
ACTIONS = ("add", "replace", "remove")
UNTRACK_MESSAGE = "[Memory] Stop versioning memory files"


class MemoryStore:
    """读写当前有效记忆；每次调用都重新读取磁盘，不缓存用户内容。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.memory_dir = self.data_dir / "memory"
        # 与资料库共用数据目录锁：判断、回顾与管理页的写入彼此串行。
        self._lock = _lock_for(self.data_dir)
        with self._lock:
            self._initialize()

    def snapshot(self) -> dict[str, dict]:
        """返回两个目标的当前内容；直接文件修改会在本次读取中生效。

        超出容量的文件照常返回（`usage.chars` 大于 `limit`），由调用方提示整理，不在读取时报错。
        """
        with self._lock:
            return {target: self._target_snapshot(target) for target in TARGETS}

    def apply(
        self,
        action: str,
        target: str,
        content: str | None = None,
        old_text: str | None = None,
        *,
        exact: bool = False,
        expected_version: str | None = None,
    ) -> dict:
        """新增、替换或删除一个条目。

        `exact` 为真时 `old_text` 必须等于某个条目全文（管理页），否则按片段唯一匹配（模型）。
        给出 `expected_version` 时，文件在读取之后被改过即拒绝，不覆盖别人的修改。
        """
        self._validate_request(action, target, content, old_text)
        with self._lock:
            current = self._target_snapshot(target)
            if expected_version is not None and expected_version != current["version"]:
                raise VersionConflictError(current["version"])
            entries = list(current["entries"])
            changed, old = self._change(entries, action, content, old_text, exact)
            serialized = self._serialize(entries)
            limit = TARGETS[target][1]
            # 已经超限的文件允许变小：整理本身不能因为容量被拒绝。
            if len(serialized) > limit and len(serialized) > current["usage"]["chars"]:
                raise MemoryFullError(target, len(serialized), limit)
            if changed:
                try:
                    self._atomic_write(self._path(target), serialized)
                except OSError as error:
                    raise MemoryStoreUnavailableError("长期记忆保存失败") from error
            return {
                "target": target,
                "action": action,
                "changed": changed,
                "entries": entries,
                "usage": {"chars": len(serialized), "limit": limit},
                "version": _version(serialized),
                "old": old,
            }

    def _initialize(self) -> None:
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            for target in TARGETS:
                path = self._path(target)
                if not path.exists():
                    self._atomic_write(path, "")
        except OSError as error:
            raise MemoryStoreUnavailableError("无法初始化长期记忆文件") from error
        self._stop_versioning()

    def _stop_versioning(self) -> None:
        """旧实例里记忆曾随数据目录仓库提交：单独提交一次移出版本管理，文件与其他暂存保持不动。

        用临时索引构造提交，不经过共享索引，资料库已暂存的改动不会被一并提交。
        失败只意味着记忆文件仍被跟踪，不影响读写，因此不抛出。
        """
        if not (self.data_dir / ".git").exists():
            return
        listed = self._git("ls-files", "--", "memory")
        if listed is None or not listed.stdout.strip():
            return
        index = self.data_dir / ".git" / "pebble-untrack-index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        try:
            ignore = self.data_dir / ".gitignore"
            if not ignore.exists() or ignore.read_text(encoding="utf-8") != GITIGNORE:
                self._atomic_write(ignore, GITIGNORE)
            steps = (
                ("read-tree", "HEAD"),
                ("rm", "-r", "--cached", "--quiet", "--", "memory"),
                ("add", "--", ".gitignore"),
            )
            if any(self._git(*step, env=env) is None for step in steps):
                return
            tree = self._git("write-tree", env=env)
            if tree is None:
                return
            commit = self._git(
                "commit-tree", tree.stdout.strip(), "-p", "HEAD", "-m", UNTRACK_MESSAGE
            )
            if commit is None or self._git("update-ref", "HEAD", commit.stdout.strip()) is None:
                return
            self._git("rm", "-r", "--cached", "--quiet", "--", "memory")
            self._git("add", "--", ".gitignore")
        except (OSError, UnicodeError):
            return
        finally:
            index.unlink(missing_ok=True)

    def _git(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str] | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.data_dir), *args],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except OSError:
            return None
        return result if result.returncode == 0 else None

    def _target_snapshot(self, target: str) -> dict:
        if target not in TARGETS:
            raise MemoryValidationError([{"field": "target", "message": "必须是 user 或 memory"}])
        path = self._path(target)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        except (OSError, UnicodeError) as error:
            raise MemoryStoreUnavailableError(f"无法读取 {path.name}") from error
        entries = self._parse(content)
        normalized = self._serialize(entries)
        return {
            "target": target,
            "content": normalized,
            "entries": entries,
            "usage": {"chars": len(normalized), "limit": TARGETS[target][1]},
            "version": _version(normalized),
        }

    @staticmethod
    def _validate_request(
        action: str, target: str, content: str | None, old_text: str | None
    ) -> None:
        errors = []
        if action not in ACTIONS:
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
        entries: list[str],
        action: str,
        content: str | None,
        old_text: str | None,
        exact: bool,
    ) -> tuple[bool, str | None]:
        """返回（是否有有效修改, 被替换或移除的原条目）；新增没有原条目。"""
        normalized = (content or "").strip()
        if action == "add":
            if normalized in entries:
                return False, None
            entries.append(normalized)
            return True, None

        needle = (old_text or "").strip()
        if exact:
            matches = [index for index, entry in enumerate(entries) if entry == needle]
        else:
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

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _version(content: str) -> str:
    """文件内容的版本号：规范化后的内容哈希，与写入方式无关。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
