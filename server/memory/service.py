"""两个 Markdown 文件组成的长期记忆。

每个目标就是一份完整的 Markdown 文档，写法不限（段落、列表、小标题）。记忆不做版本管理：
内容简短、由模型持续整理，变化本身已经体现在对话里的记忆提示中。文件只保存当前有效内容；
写入是原子替换，版本号取文件内容的哈希，供管理页做冲突检查。
"""

from __future__ import annotations

import base64
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

    def edit(self, operations: list[dict]) -> dict:
        """模型的按行编辑：`operations` 按锚点插入、替换、删除、移动整行，或在分区末尾追加。

        锚点都指调用前的内容，一次调用可以同时改两个分区，整体生效或整体失败；锚点失效
        （那一行已被改动）时报错并附两个分区带锚点的最新内容。结果的 `applied` 按顺序列出
        每处修改实际删除与写入的整行，提示据此生成。
        """
        parsed = _parse_operations(operations)
        with self._lock:
            current = {target: self._target_snapshot(target) for target in TARGETS}
            contents = {target: current[target]["content"] for target in TARGETS}
            updated, applied = _apply_operations(contents, parsed)
            changed = [target for target in TARGETS if updated[target] != contents[target]]
            if not changed:
                applied = [{**item, "changed": False} for item in applied]
            for target in changed:
                limit = TARGETS[target][1]
                size = len(updated[target])
                if size > limit and size > len(contents[target]):
                    raise MemoryFullError(target, size, limit, memory=model_view(contents))
            # 先写变长的分区：两次写入之间出错时，移动的内容最多暂时两边各有一份。
            for target in sorted(changed, key=lambda t: len(contents[t]) - len(updated[t])):
                try:
                    self._atomic_write(self._path(target), updated[target])
                except OSError as error:
                    raise MemoryStoreUnavailableError("长期记忆保存失败") from error
            view = model_view(updated)
            return {
                "changed": bool(changed),
                "applied": applied,
                "memory": {target: view[target] for target in changed},
            }

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


# ---- 模型视图与按行编辑 ----

ANCHOR_PREFIX = re.compile(r"^\s*[a-z0-9]{4}\|")
LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
EMPTY_VIEW = "（空）"
# 每种动作的必填与可选字段。
ACTIONS = {
    "append": (("target", "text"), ()),
    "insert": (("after", "text"), ()),
    "replace": (("anchor", "text"), ("end_anchor",)),
    "delete": (("anchor",), ("end_anchor",)),
    "move": (("anchor", "to"), ("end_anchor",)),
}
ANCHOR_FIELDS = ("anchor", "end_anchor", "after")


def anchors(contents: dict[str, str]) -> dict[str, list[str | None]]:
    """每个分区逐行的锚点（空行为 None）：由分区名与行文字算出，两个分区合起来唯一。

    重复行或哈希碰撞按出现顺序加盐顺延，结果只取决于内容本身。
    """
    used: set[str] = set()
    result: dict[str, list[str | None]] = {}
    for target in TARGETS:
        column: list[str | None] = []
        for line in _lines(contents[target]):
            if not line.strip():
                column.append(None)
                continue
            salt = 0
            while (anchor := _anchor(target, line.rstrip(), salt)) in used:
                salt += 1
            used.add(anchor)
            column.append(anchor)
        result[target] = column
    return result


def model_view(contents: dict[str, str]) -> dict[str, dict]:
    """两个分区给判断与回顾模型看的带锚点内容与用量。"""
    column = anchors(contents)
    return {
        target: {
            "content": _render(_lines(contents[target]), column[target]),
            "usage": {"chars": len(contents[target]), "limit": TARGETS[target][1]},
        }
        for target in TARGETS
    }


def _anchor(target: str, line: str, salt: int) -> str:
    digest = hashlib.blake2s(f"{target}\n{line}\n{salt}".encode()).digest()
    return base64.b32encode(digest).decode().lower()[:4]


def _lines(content: str) -> list[str]:
    return content.split("\n") if content else []


def _render(lines: list[str], column: list[str | None]) -> str:
    if not lines:
        return EMPTY_VIEW
    return "\n".join(
        line if anchor is None else f"{anchor}| {line}"
        for line, anchor in zip(lines, column, strict=True)
    )


def _bare(line: str) -> str:
    return LIST_MARKER.sub("", line).strip()


def _parse_operations(operations: object) -> list[dict]:
    """检查动作与字段：字段不全或新文字带锚点前缀时整次拒绝，不替模型修正。"""
    if not isinstance(operations, list) or not operations:
        raise MemoryValidationError([{"field": "operations", "message": "至少需要一处修改"}])
    errors: list[dict[str, str]] = []
    parsed = []
    for index, item in enumerate(operations, start=1):
        field = f"operations[{index}]"
        if not isinstance(item, dict):
            errors.append({"field": field, "message": f"第 {index} 处修改格式不正确"})
            continue
        action = item.get("action")
        if action not in ACTIONS:
            errors.append(
                {
                    "field": f"{field}.action",
                    "message": f"第 {index} 处：action 必须是 {'、'.join(ACTIONS)} 之一",
                }
            )
            continue
        required, optional = ACTIONS[action]
        op: dict = {"index": index, "action": action}
        for key in (*required, *optional):
            value = item.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                if key in required:
                    errors.append(
                        {
                            "field": f"{field}.{key}",
                            "message": f"第 {index} 处：{action} 需要 {key}",
                        }
                    )
                continue
            if not isinstance(value, str):
                errors.append(
                    {"field": f"{field}.{key}", "message": f"第 {index} 处：{key} 必须是字符串"}
                )
                continue
            if key in ANCHOR_FIELDS:
                value = value.strip().rstrip("|").strip().lower()
            elif key in ("target", "to"):
                value = value.strip()
                if value not in TARGETS:
                    errors.append(
                        {
                            "field": f"{field}.{key}",
                            "message": f"第 {index} 处：{key} 必须是 user 或 memory",
                        }
                    )
                    continue
            else:
                lines = [line.rstrip() for line in value.replace("\r\n", "\n").strip().split("\n")]
                if any(ANCHOR_PREFIX.match(line) for line in lines):
                    errors.append(
                        _op_error(index, "text", "text 里有锚点前缀，锚点不是记忆内容，只写原文")
                    )
                    continue
                value = lines
            op[key] = value
        parsed.append(op)
    if errors:
        raise MemoryValidationError(errors)
    return parsed


def _apply_operations(
    contents: dict[str, str], operations: list[dict]
) -> tuple[dict[str, str], list[dict]]:
    """在调用前的内容上定位全部修改，再逐分区生成新内容；任何定位错误都整次拒绝。"""
    lines = {target: _lines(contents[target]) for target in TARGETS}
    column = anchors(contents)
    where = {
        anchor: (target, row)
        for target in TARGETS
        for row, anchor in enumerate(column[target])
        if anchor is not None
    }
    errors: list[dict[str, str]] = []

    def locate(op: dict, key: str) -> tuple[str, int] | None:
        found = where.get(op[key])
        if found is None:
            errors.append(
                _op_error(
                    op["index"],
                    key,
                    f"锚点 {op[key]} 不存在（那一行可能已被改动），请按最新内容重新定位",
                )
            )
        return found

    # 第一遍：替换、删除、移动认领行范围。
    claimed: dict[tuple[str, int], dict] = {}
    ranges: dict[int, tuple[str, int, int]] = {}
    for op in operations:
        if op["action"] not in ("replace", "delete", "move"):
            continue
        start = locate(op, "anchor")
        end = locate(op, "end_anchor") if "end_anchor" in op else start
        if start is None or end is None:
            continue
        target, first = start
        if end[0] != target or end[1] < first:
            errors.append(
                _op_error(
                    op["index"], "end_anchor", "end_anchor 必须与 anchor 在同一分区且不在它之前"
                )
            )
            continue
        if op["action"] == "move" and op["to"] == target:
            errors.append(
                {
                    "field": f"operations[{op['index']}].to",
                    "message": f"第 {op['index']} 处：to 必须是另一个分区",
                }
            )
            continue
        overlap = [
            claimed[(target, row)]["index"]
            for row in range(first, end[1] + 1)
            if (target, row) in claimed
        ]
        if overlap:
            errors.append(
                {
                    "field": f"operations[{op['index']}].anchor",
                    "message": f"第 {op['index']} 处与第 {overlap[0]} 处修改了同一行",
                }
            )
            continue
        for row in range(first, end[1] + 1):
            claimed[(target, row)] = op
        ranges[op["index"]] = (target, first, end[1])

    # 第二遍：插入挂到锚点行下面。
    inserts: dict[tuple[str, int], list[dict]] = {}
    for op in operations:
        if op["action"] != "insert" or (found := locate(op, "after")) is None:
            continue
        owner = claimed.get(found)
        if owner is not None and owner["action"] != "replace":
            errors.append(
                _op_error(
                    op["index"], "after", f"插入位置那一行被第 {owner['index']} 处删除或移走了"
                )
            )
            continue
        inserts.setdefault(found, []).append(op)

    if errors:
        raise MemoryValidationError(errors, memory=model_view(contents))

    # 查重基准：每个分区调用后仍保留的行，加上替换写入的行。
    present = {target: set() for target in TARGETS}
    for target in TARGETS:
        for row, line in enumerate(lines[target]):
            if line.strip() and (target, row) not in claimed:
                present[target].add(_bare(line))
    records: dict[int, dict] = {}
    replacements: dict[int, list[str]] = {}
    for op in operations:
        if op["action"] != "replace":
            continue
        target, first, last = ranges[op["index"]]
        original = [line for line in lines[target][first : last + 1] if line.strip()]
        text = list(op["text"])
        marker = LIST_MARKER.match(original[0])
        if first == last and len(text) == 1 and marker and not LIST_MARKER.match(text[0]):
            text = [marker.group(0) + text[0].strip()]
        replacements[op["index"]] = text
        present[target].update(_bare(line) for line in text if line.strip())
        same = text == original
        records[op["index"]] = {
            "action": "replace",
            "target": target,
            "removed": original,
            "added": text,
            "changed": not same,
            **({"reason": "same"} if same else {}),
        }

    # 按列表顺序决定新增内容是否写入（同一次调用里后出现的重复只写第一处）。
    accepted: dict[int, list[str]] = {}
    appends = {target: [] for target in TARGETS}
    for op in operations:
        action = op["action"]
        if action == "replace":
            continue
        if action == "delete":
            target, first, last = ranges[op["index"]]
            removed = [line for line in lines[target][first : last + 1] if line.strip()]
            records[op["index"]] = {
                "action": "delete",
                "target": target,
                "removed": removed,
                "added": [],
                "changed": True,
            }
            continue
        if action == "move":
            source, first, last = ranges[op["index"]]
            text = [line for line in lines[source][first : last + 1] if line.strip()]
            destination = op["to"]
        else:
            text = op["text"]
            destination = where[op["after"]][0] if action == "insert" else op["target"]
        new = [_bare(line) for line in text if line.strip()]
        exists = all(line in present[destination] for line in new)
        if not exists:
            present[destination].update(new)
            accepted[op["index"]] = text
            if action != "insert":
                appends[destination].append(text)
        if action == "move":
            records[op["index"]] = {
                "action": "move",
                "target": source,
                "from": source,
                "to": destination,
                "removed": text,
                "added": [] if exists else text,
                "changed": True,
            }
        else:
            records[op["index"]] = {
                "action": action,
                "target": destination,
                "removed": [],
                "added": [] if exists else text,
                "changed": not exists,
                **({"reason": "exists"} if exists else {}),
            }

    updated = {}
    for target in TARGETS:
        out: list[str] = []
        row = 0
        while row < len(lines[target]):
            owner = claimed.get((target, row))
            if owner is None:
                out.append(lines[target][row])
                out.extend(_inserted(inserts.get((target, row), []), accepted))
                row += 1
                continue
            _, first, last = ranges[owner["index"]]
            if owner["action"] == "replace":
                out.extend(replacements[owner["index"]])
                for inner in range(first, last + 1):
                    out.extend(_inserted(inserts.get((target, inner), []), accepted))
            row = last + 1
        for text in appends[target]:
            while out and not out[-1].strip():
                out.pop()
            if (
                out
                and LIST_MARKER.match(out[-1])
                and all(LIST_MARKER.match(line) for line in text if line.strip())
            ):
                out.extend(text)
            elif out:
                out.extend(["", *text])
            else:
                out.extend(text)
        updated[target] = _normalize("\n".join(out))
    return updated, [records[op["index"]] for op in operations]


def _op_error(index: int, key: str, message: str) -> dict[str, str]:
    return {"field": f"operations[{index}].{key}", "message": f"第 {index} 处：{message}"}


def _inserted(ops: list[dict], accepted: dict[int, list[str]]) -> list[str]:
    return [line for op in ops for line in accepted.get(op["index"], [])]
