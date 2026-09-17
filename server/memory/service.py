"""两个 Markdown 文件组成的长期记忆。

每个目标就是一份完整的 Markdown 文档，写法不限（段落、列表、小标题）。记忆不做版本管理：
内容简短、由模型持续整理，变化本身已经体现在对话里的记忆提示中。文件只保存当前有效内容；
写入是原子替换，版本号取文件内容的哈希，供管理页做冲突检查。
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

TARGETS = {
    "user": ("USER.md", 1375),
    "memory": ("MEMORY.md", 2200),
}
UNTRACK_MESSAGE = "[Memory] Stop versioning memory files"
# 旧格式按条保存时的分隔：启动时换成空行，每条变成一段。
LEGACY_DELIMITER = "\n\n§\n\n"
TARGET_ERROR = {"field": "target", "message": "必须是 user 或 memory"}


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

    def edit(self, target: str, old_text: str = "", new_text: str = "") -> dict:
        """模型的片段编辑：`old_text` 为空时追加，`new_text` 为空时删除，都有时替换。

        `old_text` 必须在文档中恰好出现一次；要追加的内容已经原样在文档里时不写入。
        """
        old, new = (old_text or "").strip(), (new_text or "").strip()
        errors = [TARGET_ERROR] if target not in TARGETS else []
        if not old and not new:
            errors.append({"field": "new_text", "message": "old_text 与 new_text 不能都为空"})
        if errors:
            raise MemoryValidationError(errors)
        with self._lock:
            current = self._target_snapshot(target)
            content = current["content"]
            if not old:
                if new in content:
                    return self._result(current, changed=False)
                return self._save(current, f"{content}\n\n{new}" if content else new)
            count = content.count(old)
            if count != 1:
                reason = (
                    "没有找到这段原文" if count == 0 else "这段原文出现了多次，请给出更长的片段"
                )
                raise MemoryValidationError([{"field": "old_text", "message": reason}])
            if old == new:
                return self._result(current, changed=False)
            return self._save(current, content.replace(old, new))

    def write(self, target: str, content: str, *, expected_version: str) -> dict:
        """管理页整份保存：文件在读取之后被改过即拒绝，不覆盖别人的修改。"""
        if target not in TARGETS:
            raise MemoryValidationError([TARGET_ERROR])
        with self._lock:
            current = self._target_snapshot(target)
            if expected_version != current["version"]:
                raise VersionConflictError(current["version"])
            return self._save(current, content)

    def _save(self, current: dict, content: str) -> dict:
        target = current["target"]
        normalized = _normalize(content)
        if normalized == current["content"]:
            return self._result(current, changed=False)
        limit = TARGETS[target][1]
        # 已经超限的文件允许变小：整理本身不能因为容量被拒绝。
        if len(normalized) > limit and len(normalized) > current["usage"]["chars"]:
            raise MemoryFullError(target, len(normalized), limit)
        try:
            self._atomic_write(self._path(target), normalized)
        except OSError as error:
            raise MemoryStoreUnavailableError("长期记忆保存失败") from error
        return self._result(self._describe(target, normalized), changed=True)

    @staticmethod
    def _result(snapshot: dict, *, changed: bool) -> dict:
        return {**snapshot, "changed": changed}

    def _initialize(self) -> None:
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            for target in TARGETS:
                path = self._path(target)
                if not path.exists():
                    self._atomic_write(path, "")
                    continue
                content = path.read_text(encoding="utf-8")
                if LEGACY_DELIMITER in content:
                    self._atomic_write(path, _normalize(content.replace(LEGACY_DELIMITER, "\n\n")))
        except (OSError, UnicodeError) as error:
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
            raise MemoryValidationError([TARGET_ERROR])
        path = self._path(target)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        except (OSError, UnicodeError) as error:
            raise MemoryStoreUnavailableError(f"无法读取 {path.name}") from error
        return self._describe(target, _normalize(content))

    @staticmethod
    def _describe(target: str, content: str) -> dict:
        return {
            "target": target,
            "content": content,
            "usage": {"chars": len(content), "limit": TARGETS[target][1]},
            "version": _version(content),
        }

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


def _normalize(content: str) -> str:
    """统一换行、去掉首尾空白，删除片段后留下的多余空行合并为一个。"""
    text = content.replace("\r\n", "\n").strip()
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)


def _version(content: str) -> str:
    """文件内容的版本号：规范化后的内容哈希，与写入方式无关。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
